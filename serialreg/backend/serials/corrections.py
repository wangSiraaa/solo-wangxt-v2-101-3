"""发行更正单领域服务。

所有投影变更都集中在这里，视图/序列化器只做参数解析：

* ``draft_correction``  开单：封存原关系快照（before），登记拟议关系（after）
* ``revise_draft``      改单：重封存 before（以最新投影为准）
* ``apply_order``       应用：单事务重写编号关联与月份，版本号 +1
* ``withdraw_order``    撤回：草稿仅置状态；已应用单按 before 反向重放

重复 apply/withdraw 是幂等重放（记审计、不改投影）；乱序（投影已被后续
更正/改动改变）抛 ``CorrectionConflict``（HTTP 409），不留任何半成品。
"""
from collections import Counter

from django.db import transaction
from django.utils import timezone

from .models import (
    CorrectionEvent, CorrectionLine, CorrectionNumberSnapshot,
    CorrectionOrder, Issue, IssueNumber, IssueNumbering,
)


class CorrectionValidationError(Exception):
    """更正内容结构性/业务性不合法 → HTTP 400。"""

    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


class CorrectionConflict(Exception):
    """状态冲突或乱序到达 → HTTP 409，整单不动。"""

    def __init__(self, detail, code="conflict"):
        self.detail = detail
        self.code = code
        super().__init__(str(detail))


class CorrectionStateError(Exception):
    """不允许的状态迁移（如撤回已撤回单）→ HTTP 409。"""

    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


# ---------- 快照工具 ----------

def _numbering_snapshot(issue):
    """读取某发行期当前投影：编号关联（按 sort_key 稳定排序）与月份。"""
    rows = list(
        IssueNumbering.objects.filter(issue=issue)
        .select_related("number")
        .order_by("number__sort_key", "number__id")
    )
    return {
        "numberings": rows,
        "month_start": issue.issue_month,
        "month_end": issue.issue_month_end,
        "kind": issue.kind,
    }


def _save_number_snapshots(line, side, numberings):
    CorrectionNumberSnapshot.objects.bulk_create([
        CorrectionNumberSnapshot(
            line=line, side=side,
            number=nn.number,
            volume=nn.number.volume,
            number_label=nn.number.number,
            cover_label=nn.label or "",
        )
        for nn in numberings
    ])


def _snapshot_number_ids(line, side):
    return list(
        CorrectionNumberSnapshot.objects.filter(line=line, side=side)
        .exclude(number=None)
        .values_list("number_id", flat=True)
    )


# ---------- 开单 / 改单 ----------

def draft_correction(title, reason, proposals, client_ref=""):
    """按 proposals 建草稿更正单。

    proposals: [{"issue": Issue, "kind": str|None,
                 "issue_month": date, "issue_month_end": date|None,
                 "number_ids": [int,...]} ...]
    编号/月份可以只改其一：number_ids 缺省表示不动编号，月份缺省表示不动月份。
    """
    if client_ref:
        existing = CorrectionOrder.objects.filter(
            title=title, client_ref=client_ref,
        ).first()
        if existing:
            # 幂等：同一 client_ref 的重复提交直接返回旧单
            return existing, False

    proposals = _normalize_proposals(title, proposals)
    with transaction.atomic():
        order = CorrectionOrder.objects.create(
            title=title, reason=reason, client_ref=client_ref or "",
        )
        _build_lines(order, proposals)
        CorrectionEvent.objects.create(
            order=order, action=CorrectionEvent.Action.CREATE,
            detail=f"开单，{len(proposals)} 条更正行",
        )
    return order, True


def revise_draft(order, reason, proposals, client_ref=""):
    if order.status != CorrectionOrder.Status.DRAFT:
        raise CorrectionStateError(f"更正单#{order.id}不是草稿，不能修改。")
    proposals = _normalize_proposals(order.title, proposals)
    with transaction.atomic():
        order.lines.all().delete()  # 级联删除两侧快照
        order.reason = reason
        if client_ref:
            order.client_ref = client_ref
        order.save(update_fields=["reason", "client_ref"])
        _build_lines(order, proposals)
        CorrectionEvent.objects.create(
            order=order, action=CorrectionEvent.Action.UPDATE,
            detail=f"改单，{len(proposals)} 条更正行",
        )
    return order


def _normalize_proposals(title, proposals):
    if not proposals:
        raise CorrectionValidationError("更正单至少包含一条更正行。")
    normalized = []
    seen_issues = set()
    for raw in proposals:
        issue = raw["issue"]
        if issue.title_id != title.id:
            raise CorrectionValidationError(
                f"发行期 #{issue.id} 不属于该刊，不能跨刊更正。",
            )
        if issue.id in seen_issues:
            raise CorrectionValidationError(
                f"发行期 #{issue.id} 在单内重复出现，请合并为一条更正行。",
            )
        seen_issues.add(issue.id)

        current = _numbering_snapshot(issue)
        cur_ids = [nn.number_id for nn in current["numberings"]]

        if raw.get("number_ids") is not None:
            number_ids = list(raw["number_ids"])
            if not number_ids:
                raise CorrectionValidationError(
                    f"发行期 #{issue.id} 至少保留一个覆盖编号。",
                )
            if len(set(number_ids)) != len(number_ids):
                raise CorrectionValidationError(
                    f"发行期 #{issue.id} 的拟议编号不能重复。",
                )
            after_numbers = list(
                IssueNumber.objects.filter(id__in=number_ids)
                .order_by("sort_key", "id"),
            )
            if len(after_numbers) != len(number_ids):
                raise CorrectionValidationError("拟议编号中有不存在的期号。")
            wrong = [n.id for n in after_numbers if n.title_id != title.id]
            if wrong:
                raise CorrectionValidationError(
                    f"期号 {wrong} 不属于该刊。",
                )
        else:
            after_numbers = [nn.number for nn in current["numberings"]]

        kind = raw.get("kind") or current["kind"]
        if kind == Issue.IssueKind.COMBINED and len(after_numbers) < 2:
            raise CorrectionValidationError(
                f"发行期 #{issue.id} 标记为合刊必须覆盖至少两个期号。",
            )
        if kind == Issue.IssueKind.REGULAR and len(after_numbers) != 1:
            raise CorrectionValidationError(
                f"发行期 #{issue.id} 标记为普通期只能覆盖一个期号。",
            )

        month_start = raw.get("issue_month") or current["month_start"]
        if "issue_month_end" in raw:
            month_end = raw.get("issue_month_end")
        else:
            month_end = current["month_end"]
        if not month_start:
            raise CorrectionValidationError(
                f"发行期 #{issue.id} 必须有发行起始月。",
            )
        if month_end and month_end < month_start:
            raise CorrectionValidationError(
                f"发行期 #{issue.id} 的截止年月不能早于起始年月。",
            )

        normalized.append({
            "issue": issue,
            "kind_after": kind,
            "before": current,
            "after_numbers": after_numbers,
            "labels": dict(raw.get("labels") or {}),
            "month_start_after": month_start,
            "month_end_after": month_end,
            "current_ids": cur_ids,
        })

    # 单内横向校验：拟议侧同一编号不能分给两个发行期
    plan = Counter()
    for p in normalized:
        plan.update(n.id for n in p["after_numbers"])
    duplicated = [nid for nid, c in plan.items() if c > 1]
    if duplicated:
        raise CorrectionValidationError(
            f"拟议编号 {sorted(duplicated)} 在单内被多个发行期同时占用，"
            "请在同一行/同一单内显式调整。",
        )
    return normalized


def _build_lines(order, proposals):
    """按最新投影封存 before，并写入 after；顺带做占用预检。"""
    target_issue_ids = {p["issue"].id for p in proposals}
    for p in proposals:
        issue = p["issue"]
        before = p["before"]
        line = CorrectionLine.objects.create(
            order=order, issue=issue,
            kind_before=before["kind"], kind_after=p["kind_after"],
            month_start_before=before["month_start"],
            month_end_before=before["month_end"],
            month_start_after=p["month_start_after"],
            month_end_after=p["month_end_after"],
        )
        _save_number_snapshots(line, CorrectionNumberSnapshot.Side.BEFORE,
                               before["numberings"])
        # 拟议侧快照：此时关联尚不存在，用轻量对象承载 label 生成
        after_nns = _planned_numberings(
            issue, p["after_numbers"], raw_labels=p.get("labels") or {},
        )
        _save_number_snapshots(line, CorrectionNumberSnapshot.Side.AFTER, after_nns)

        # 占用预检：编号被「本单不处理的其他发行期」占用 → 整单拒绝
        foreign = (
            IssueNumbering.objects.filter(
                number_id__in=[n.id for n in p["after_numbers"]],
            )
            .exclude(issue_id__in=target_issue_ids)
            .select_related("number", "issue")
        )
        blockers = [
            f"v.{nn.number.volume or '—'}no.{nn.number.number} 已属于发行期#{nn.issue_id}"
            for nn in foreign
        ]
        if blockers:
            raise CorrectionValidationError(
                f"发行期 #{issue.id} 的拟议编号冲突：" + "；".join(blockers)
                + "。整张更正单已拒绝（未写入任何关联）。",
            )


def _planned_numberings(issue, numbers, raw_labels):
    """生成拟议编号关联的内存对象（不写库），用于快照与统一落库。"""
    raw = raw_labels or {}
    labels = {}
    for n in numbers:  # 兼容 JSON 的字符串键
        val = raw.get(n.id, raw.get(str(n.id), ""))
        labels[n.id] = str(val) if val else ""
    joined = "-".join(n.number for n in numbers)
    result = []
    for n in numbers:
        label = labels.get(n.id) or (f"no.{joined}" if len(numbers) > 1 else "")
        result.append(IssueNumbering(issue=issue, number=n, label=label))
    return result


# ---------- 应用 ----------

def _assert_before_matches(line, issue):
    """应用/撤回前确认实时投影仍等于封存的 Before（乱序防护）。"""
    current = _numbering_snapshot(issue)
    current_ids = sorted(nn.number_id for nn in current["numberings"])
    if current_ids != sorted(_snapshot_number_ids(
        line, CorrectionNumberSnapshot.Side.BEFORE,
    )):
        raise CorrectionConflict(
            f"发行期 #{issue.id} 的当前编号关系与更正单#{line.order_id}封存的"
            "原关系不一致：期间已有其他更正/改动，请基于最新投影重新开单。",
            code="stale_numbers",
        )
    if current["month_start"] != line.month_start_before or (
        current["month_end"] != line.month_end_before
    ):
        raise CorrectionConflict(
            f"发行期 #{issue.id} 的当前发行区间与更正单#{line.order_id}封存的"
            "原区间不一致（乱序到达），请基于最新投影重新开单。",
            code="stale_months",
        )


def apply_order(order):
    """应用更正单。返回 (order, changed: bool)。重复应用幂等。"""
    if order.status == CorrectionOrder.Status.WITHDRAWN:
        raise CorrectionStateError(
            f"更正单#{order.id}已撤回，不能应用；请重新开单。",
        )
    if order.status == CorrectionOrder.Status.APPLIED:
        # 幂等重放：不重复改变关系，只补一条审计
        CorrectionEvent.objects.create(
            order=order, action=CorrectionEvent.Action.APPLY_REPLAY,
            detail="重复应用请求，投影未再变更",
        )
        return order, False

    with transaction.atomic():
        # 锁单 + 锁所有目标发行期，避免并发应用互相覆盖
        locked = (
            CorrectionOrder.objects.select_for_update().filter(pk=order.pk)
        )
        list(locked)
        lines = list(order.lines.all())
        issues = {
            i.id: i for i in
            Issue.objects.select_for_update().filter(
                id__in=[ln.issue_id for ln in lines],
            )
        }
        target_ids = set(issues)

        # 1) 逐行确认投影仍停留在 before；同时锁定单内涉及的全部编号行，
        #    防止并发入藏/更正把同一编号插到别的发行期上
        locked_number_ids = set()
        for line in lines:
            _assert_before_matches(line, issues[line.issue_id])
            locked_number_ids.update(_snapshot_number_ids(
                line, CorrectionNumberSnapshot.Side.BEFORE,
            ))
            locked_number_ids.update(_snapshot_number_ids(
                line, CorrectionNumberSnapshot.Side.AFTER,
            ))
        list(
            IssueNumber.objects.select_for_update()
            .filter(id__in=locked_number_ids)
        )

        # 2) 逐行确认拟议编号没有被本单外的发行期占用
        for line in lines:
            after_ids = _snapshot_number_ids(
                line, CorrectionNumberSnapshot.Side.AFTER,
            )
            foreign = list(
                IssueNumbering.objects.filter(number_id__in=after_ids)
                .exclude(issue_id__in=target_ids)
                .select_related("number"),
            )
            if foreign:
                detail = "；".join(
                    f"v.{nn.number.volume or '—'}no.{nn.number.number} 已属于发行期#{nn.issue_id}"
                    for nn in foreign
                )
                raise CorrectionConflict(
                    f"编号冲突：{detail}。整张更正单被拒绝，未留下半条关联。",
                    code="number_occupied",
                )

        # 3) 整单重写投影：先删全部目标关联，再按 after 快照统一重建
        now = timezone.now()
        IssueNumbering.objects.filter(issue_id__in=target_ids).delete()
        new_rows = []
        for line in lines:
            issue = issues[line.issue_id]
            version_before = issue.version
            after_snaps = list(
                CorrectionNumberSnapshot.objects.filter(
                    line=line, side=CorrectionNumberSnapshot.Side.AFTER,
                ).select_related("number"),
            )
            for snap in after_snaps:
                new_rows.append(IssueNumbering(
                    issue=issue, number=snap.number, label=snap.cover_label,
                ))
            issue.kind = line.kind_after
            issue.issue_month = line.month_start_after
            issue.issue_month_end = line.month_end_after
            issue.version = version_before + 1
            issue.last_corrected_at = now
            line.version_before = version_before
            line.version_after = issue.version
            line.save(update_fields=["version_before", "version_after"])
        IssueNumbering.objects.bulk_create(new_rows)
        Issue.objects.bulk_update(
            list(issues.values()),
            ["kind", "issue_month", "issue_month_end", "version",
             "last_corrected_at"],
        )

        order.status = CorrectionOrder.Status.APPLIED
        order.applied_at = now
        order.save(update_fields=["status", "applied_at"])
        CorrectionEvent.objects.create(
            order=order, action=CorrectionEvent.Action.APPLY,
            detail=f"应用成功，{len(lines)} 行投影已重写",
        )
    return order, True


# ---------- 撤回 ----------

def withdraw_order(order):
    """撤回更正单。返回 (order, changed: bool)。重复撤回幂等。"""
    if order.status == CorrectionOrder.Status.DRAFT:
        with transaction.atomic():
            list(CorrectionOrder.objects.select_for_update().filter(pk=order.pk))
            order.status = CorrectionOrder.Status.WITHDRAWN
            order.withdrawn_at = timezone.now()
            order.save(update_fields=["status", "withdrawn_at"])
            CorrectionEvent.objects.create(
                order=order, action=CorrectionEvent.Action.WITHDRAW,
                detail="草稿撤回，投影未发生过变更",
            )
        return order, True

    if order.status == CorrectionOrder.Status.WITHDRAWN:
        CorrectionEvent.objects.create(
            order=order, action=CorrectionEvent.Action.WITHDRAW_REPLAY,
            detail="重复撤回请求，投影未再变更",
        )
        return order, False

    with transaction.atomic():
        list(CorrectionOrder.objects.select_for_update().filter(pk=order.pk))
        lines = list(order.lines.all())
        issues = {
            i.id: i for i in
            Issue.objects.select_for_update().filter(
                id__in=[ln.issue_id for ln in lines],
            )
        }
        target_ids = set(issues)

        # 乱序防护 1：当前投影必须仍是 after；并锁定涉及编号防并发
        locked_number_ids = set()
        for line in lines:
            issue = issues[line.issue_id]
            current = _numbering_snapshot(issue)
            current_ids = sorted(nn.number_id for nn in current["numberings"])
            if current_ids != sorted(_snapshot_number_ids(
                line, CorrectionNumberSnapshot.Side.AFTER,
            )):
                raise CorrectionConflict(
                    f"发行期 #{issue.id} 在更正单#{order.id}应用后又被其他更正"
                    "改动，不能按本单撤回；请基于最新投影处理。",
                    code="superseded",
                )
            if (current["month_start"] != line.month_start_after or
                    current["month_end"] != line.month_end_after):
                raise CorrectionConflict(
                    f"发行期 #{issue.id} 在更正单#{order.id}应用后发行区间又被"
                    "改动，不能按本单撤回。",
                    code="superseded",
                )
            # 乱序防护 2：版本号必须停在应用时的版本
            if line.version_after is not None and issue.version != line.version_after:
                raise CorrectionConflict(
                    f"发行期 #{issue.id} 当前版本 v{issue.version} 已超过本单应用时"
                    f"版本 v{line.version_after}，撤回属于乱序操作，已拒绝。",
                    code="version_mismatch",
                )
            locked_number_ids.update(_snapshot_number_ids(
                line, CorrectionNumberSnapshot.Side.BEFORE,
            ))
            locked_number_ids.update(_snapshot_number_ids(
                line, CorrectionNumberSnapshot.Side.AFTER,
            ))
        list(
            IssueNumber.objects.select_for_update()
            .filter(id__in=locked_number_ids)
        )

        # 编号不能静默挪走：原编号若被本单外的发行期占用，拒绝反向重放
        for line in lines:
            before_ids = _snapshot_number_ids(
                line, CorrectionNumberSnapshot.Side.BEFORE,
            )
            foreign = list(
                IssueNumbering.objects.filter(number_id__in=before_ids)
                .exclude(issue_id__in=target_ids)
                .select_related("number"),
            )
            if foreign:
                detail = "；".join(
                    f"v.{nn.number.volume or '—'}no.{nn.number.number} 现属于发行期#{nn.issue_id}"
                    for nn in foreign
                )
                raise CorrectionConflict(
                    f"撤回会把编号静默挪给其他发行期：{detail}。撤回被拒绝。",
                    code="number_occupied",
                )

        now = timezone.now()
        IssueNumbering.objects.filter(issue_id__in=target_ids).delete()
        restore_rows = []
        for line in lines:
            issue = issues[line.issue_id]
            before_snaps = list(
                CorrectionNumberSnapshot.objects.filter(
                    line=line, side=CorrectionNumberSnapshot.Side.BEFORE,
                ).select_related("number"),
            )
            for snap in before_snaps:
                restore_rows.append(IssueNumbering(
                    issue=issue, number=snap.number, label=snap.cover_label,
                ))
            issue.kind = line.kind_before
            issue.issue_month = line.month_start_before
            issue.issue_month_end = line.month_end_before
            issue.version += 1
            issue.last_corrected_at = now
        IssueNumbering.objects.bulk_create(restore_rows)
        Issue.objects.bulk_update(
            list(issues.values()),
            ["kind", "issue_month", "issue_month_end", "version",
             "last_corrected_at"],
        )

        order.status = CorrectionOrder.Status.WITHDRAWN
        order.withdrawn_at = now
        order.save(update_fields=["status", "withdrawn_at"])
        CorrectionEvent.objects.create(
            order=order, action=CorrectionEvent.Action.WITHDRAW,
            detail=f"撤回成功，{len(lines)} 行投影已还原到更正前",
        )
    return order, True


# ---------- 后续入藏 / 装订的冲突提示 ----------

def pending_draft_issue_ids(title_id, issue_ids):
    """返回这些发行期中仍挂着草稿更正单的 id 集合。"""
    if not issue_ids:
        return set()
    return set(
        CorrectionLine.objects.filter(
            issue_id__in=issue_ids,
            order__title_id=title_id,
            order__status=CorrectionOrder.Status.DRAFT,
        ).values_list("issue_id", flat=True).distinct()
    )


# ---------- 时间轴 / 导出辅助 ----------

def correction_index_for_issues(issue_ids):
    """批量取若干发行期的更正信息，供时间轴展示当前值与历史值。

    返回 {issue_id: {"current": {...}|None, "history": [...]}}。
    """
    result = {i: {"current": None, "history": []} for i in issue_ids}
    if not issue_ids:
        return result
    lines = (
        CorrectionLine.objects.filter(issue_id__in=issue_ids)
        .select_related("order")
        .prefetch_related("number_snapshots")
        .order_by("order__created_at", "id")
    )
    for line in lines:
        entry = line_payload(line)
        status = line.order.status
        result[line.issue_id]["history"].append(entry)
        if status == CorrectionOrder.Status.APPLIED:
            result[line.issue_id]["current"] = entry
    return result


def line_payload(line):
    snaps = list(line.number_snapshots.all())

    def side(side_choice):
        return [
            {"number_id": s.number_id, "volume": s.volume,
             "number": s.number_label, "label": s.cover_label}
            for s in snaps if s.side == side_choice
        ]

    return {
        "order_id": line.order_id,
        "status": line.order.status,
        "reason": line.order.reason,
        "created_at": line.order.created_at,
        "applied_at": line.order.applied_at,
        "withdrawn_at": line.order.withdrawn_at,
        "version_before": line.version_before,
        "version_after": line.version_after,
        "before": {
            "kind": line.kind_before,
            "issue_month": line.month_start_before,
            "issue_month_end": line.month_end_before,
            "numbers": side(CorrectionNumberSnapshot.Side.BEFORE),
        },
        "after": {
            "kind": line.kind_after,
            "issue_month": line.month_start_after,
            "issue_month_end": line.month_end_after,
            "numbers": side(CorrectionNumberSnapshot.Side.AFTER),
        },
    }
