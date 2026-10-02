"""连续出版物登记领域模型。

四层结构：
  Title        连续出版物（书目层，可标记停刊）
  IssueNumber  卷期编号（一个编号槽位，跨发行月的跨年卷按卷+期唯一）
  Issue        发行实体（一次出版行为；普通期挂一个编号，合刊挂多个编号）
  Item         馆内实物（一条条码=一个实物，不允许一条条码代表多个合刊期号关系）
  Binding      装订册（多个 Item 装订在一起，拆订后 Item 恢复各自位置）

两条易混的业务规则分开表达：
  缺号 = IssueNumber 没有对应 Issue（没有发行记录），不自动等于缺藏；
  缺藏 = 该编号已发行（存在 Issue），但没有入库 Item 或 Item 丢失。
"""
from django.db import models, transaction
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils import timezone


class Title(models.Model):
    """连续出版物刊名。"""

    class PublicationStatus(models.TextChoices):
        ACTIVE = "active", "在刊"
        CEASED = "ceased", "停刊"

    title = models.CharField("刊名", max_length=255)
    issn = models.CharField("ISSN", max_length=9, blank=True)
    publisher = models.CharField("出版者", max_length=255, blank=True)
    status = models.CharField(
        "出版状态", max_length=10,
        choices=PublicationStatus.choices, default=PublicationStatus.ACTIVE,
    )
    # 停刊月份：与卷期编号分开记录，只表示出版停止，不改变任何馆藏状态
    ceased_month = models.DateField("停刊月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.status == self.PublicationStatus.CEASED and not self.ceased_month:
            raise ValidationError({"ceased_month": "停刊刊名必须填写停刊月份。"})


class IssueNumber(models.Model):
    """卷期编号（编号槽位），与发行年月解耦。

    跨年卷：同一卷可以跨自然年，例如 v.60 no.3 印的是 2023-12、2024-01，
    编号仍只有一条 (volume=60, number=3)，发行时间记录在 Issue 上。
    """

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="numbers",
    )
    volume = models.CharField("卷", max_length=20, blank=True)
    number = models.CharField("期", max_length=20)
    sort_key = models.PositiveIntegerField(
        "排序键", default=0,
        help_text="馆员录入的编号顺序，跨年卷按编号顺序而非月份排列",
    )

    class Meta:
        verbose_name = "期号"
        unique_together = ("title", "volume", "number")
        ordering = ["sort_key", "id"]

    def __str__(self):
        return f"{self.volume}({self.number})" if self.volume else self.number


class Issue(models.Model):
    """一次发行。普通期关联一个 IssueNumber；两期合刊关联两个（或更多）。"""

    class IssueKind(models.TextChoices):
        REGULAR = "regular", "普通期"
        COMBINED = "combined", "合刊"

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="issues",
    )
    kind = models.CharField(
        "类型", max_length=10,
        choices=IssueKind.choices, default=IssueKind.REGULAR,
    )
    # 发行年月与卷期编号分开录入
    issue_month = models.DateField("发行年月", help_text="只取年月；合刊可只填起始月")
    issue_month_end = models.DateField(
        "发行截止年月", null=True, blank=True, help_text="合刊/跨年卷的覆盖结束月",
    )
    numbers = models.ManyToManyField(
        IssueNumber, through="IssueNumbering", related_name="issues",
    )
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # 覆盖关系（期号集合 + 发行区间）的版本号：每次应用/撤回更正单 +1。
    # 已装订、定位证据、导出快照按版本号固化，不能静默覆盖历史。
    coverage_version = models.PositiveIntegerField("覆盖版本", default=1)

    class Meta:
        verbose_name = "发行期"
        ordering = ["issue_month", "id"]

    def __str__(self):
        nums = "·".join(str(n) for n in self.numbers.all())
        return f"{self.title.title} {nums}"

    def clean(self):
        if self.issue_month_end and self.issue_month_end < self.issue_month:
            raise ValidationError({"issue_month_end": "截止年月不能早于起始年月。"})


class IssueNumbering(models.Model):
    """Issue ↔ IssueNumber 关联表。

    合刊的两个期号必须是两条独立关联记录，而不是把 3-4 塞进一个条码字段。
    """

    issue = models.ForeignKey(
        Issue, on_delete=models.CASCADE, related_name="numberings",
    )
    number = models.ForeignKey(
        IssueNumber, on_delete=models.CASCADE, related_name="numberings",
    )
    label = models.CharField("封面标识", max_length=40, blank=True,
                             help_text="如 no.3-4，仅作展示")

    class Meta:
        unique_together = ("issue", "number")


class Item(models.Model):
    """馆内实物（册）。一个条码 = 一个实物。"""

    class ItemStatus(models.TextChoices):
        AVAILABLE = "available", "在馆"
        CHECKED_OUT = "checked_out", "借出"
        LOST = "lost", "丢失"
        BOUND = "bound", "已装订"

    barcode = models.CharField("条码", max_length=40, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="items",
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT, related_name="items",
        help_text="实物对应的发行期；合刊实物只指向这一个 Issue，"
                  "对多个期号的覆盖由 IssueNumbering 表达",
    )
    # 未装订时的实际位置；装订后以 binding 的 location 为准
    location = models.CharField("馆藏位置", max_length=100, blank=True)
    status = models.CharField(
        "馆藏状态", max_length=12,
        choices=ItemStatus.choices, default=ItemStatus.AVAILABLE,
    )
    accessioned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["barcode"]

    @property
    def is_bound(self):
        return hasattr(self, "binding_entry")

    def current_location(self):
        """装订后返回装订册位置，否则返回自身位置。"""
        entry = getattr(self, "binding_entry", None)
        if entry is not None:
            return entry.binding.location
        return self.location

    def __str__(self):
        return self.barcode


class Binding(models.Model):
    """装订册：把若干已入藏实物装订在一起，实物身份与条码不变。"""

    call_number = models.CharField("装订索书号", max_length=60, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="bindings",
    )
    location = models.CharField("装订后位置", max_length=100)
    bound_month = models.DateField("装订月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    items = models.ManyToManyField(Item, through="BindingEntry", related_name="bindings")

    class Meta:
        verbose_name = "装订册"
        ordering = ["call_number"]

    def __str__(self):
        return self.call_number


class BindingEntry(models.Model):
    item = models.OneToOneField(
        Item, on_delete=models.CASCADE, related_name="binding_entry",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.CASCADE, related_name="entries",
    )
    # 装订时封存该实物原位置，拆订后恢复
    previous_location = models.CharField("装订前位置", max_length=100, blank=True)
    bound_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("item", "binding")

    def clean(self):
        if self.item_id and self.item.title_id != self.binding.title_id:
            raise ValidationError("装订册内的实物必须属于同一种刊。")


def number_holding_status(title, number):
    """计算某个期号的馆藏视图状态。

    issued+held       已发行且有在馆实物（含装订）
    issued+missing    已发行但缺藏（无实物或全部丢失/借出按调用方再细分）
    not_published     缺号：没有任何发行记录，不自动等同缺藏
    ceased_gap        停刊后出现的编号（永远不会有发行）
    """
    issues = list(number.issues.prefetch_related("items"))
    if not issues:
        ceased = title.ceased_month
        if title.status == Title.PublicationStatus.CEASED and ceased:
            return "ceased_gap"
        return "not_published"
    items = [it for iss in issues for it in iss.items.all()]
    held = any(it.status != Item.ItemStatus.LOST for it in items)
    return "issued+held" if held else "issued+missing"


def locate_number(number):
    """从任一期号找到其所在实物与实际位置（合刊、装订都可命中）。"""
    rows = []
    for issue in number.issues.all():
        for item in issue.items.select_related("title"):
            rows.append({
                "issue_id": issue.id,
                "coverage_version": issue.coverage_version,
                "barcode": item.barcode,
                "status": item.status,
                "location": item.current_location(),
                "bound": item.is_bound,
                "binding": item.binding_entry.binding.call_number if item.is_bound else None,
            })
    return rows


class CorrectionConflict(Exception):
    """更正流程的领域冲突：乱序、编号被占、存在草稿等。

    由视图层映射为 HTTP 409，与「输入不合法」的 400 区分开。
    """

    def __init__(self, detail, code="conflict", conflicts=None):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.conflicts = conflicts or []


class IssueCorrection(models.Model):
    """发行更正单：对一次发行（Issue）的覆盖期号与发行区间的拟议更正。

    更正单不就地抹掉历史：原覆盖关系保存在 original_line_set 快照里，
    拟议关系保存在 correction_line_set；应用时才改写 Issue 的现行投影，
    并把 Issue.coverage_version 推进。撤回时凭 original_* 快照还原。
    合刊的多条期号关联属于同一张更正单，校验与应用是一个事务。
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        APPLIED = "applied", "已应用"
        WITHDRAWN = "withdrawn", "已撤回"

    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT,
        related_name="corrections",
        help_text="被更正的发行实体；实体身份与装订/条码关系不随更正改变",
    )
    status = models.CharField(
        "状态", max_length=10, choices=Status.choices, default=Status.DRAFT,
    )
    reason = models.CharField("更正原因", max_length=255)
    # 拟议发行区间（应用后写入 Issue；行级数据里不再存月份）
    proposed_issue_month = models.DateField("拟议发行年月")
    proposed_issue_month_end = models.DateField(
        "拟议发行截止年月", null=True, blank=True,
    )
    # 应用前快照：即使撤回/再应用，也能还原「应用前内容」
    original_numbers = models.JSONField(
        "原覆盖编号快照", default=list,
        help_text="[{number_id, volume, number, label}, ...]",
    )
    original_issue_month = models.DateField("原发行年月", null=True, blank=True)
    original_issue_month_end = models.DateField(
        "原发行截止年月", null=True, blank=True,
    )
    applied_issue_version = models.PositiveIntegerField(
        "应用时版本", null=True, blank=True,
        help_text="本次更正应用后 Issue 的 coverage_version",
    )
    withdrawn_issue_version = models.PositiveIntegerField(
        "撤回后版本", null=True, blank=True,
    )
    # 乐观锁/乱序检测：每次状态推进 +1；客户端必须带回它看到的版本
    version = models.PositiveIntegerField("更正单版本", default=1)
    # 幂等令牌：同一业务意图（含「重复点击」）只落一张单/一次状态变更
    client_token = models.CharField(
        "幂等令牌", max_length=64, unique=True, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "发行更正单"
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["issue"],
                condition=Q(status="draft"),
                name="uniq_draft_correction_per_issue",
            ),
        ]

    def __str__(self):
        return f"更正单#{self.id}（Issue {self.issue_id}，{self.get_status_display()}）"


class CorrectionLine(models.Model):
    """更正单行：一对（原编号, 拟议编号）。拟议编号可空，表示该覆盖被撤销。

    合刊一行一条，随整张更正单一起校验/应用/撤回——不存在半条关联。
    number 用 PROTECT：编号一旦进入更正历史就不能删除，以保证可还原。
    """

    correction = models.ForeignKey(
        IssueCorrection, on_delete=models.CASCADE, related_name="lines",
    )
    original_number = models.ForeignKey(
        IssueNumber, on_delete=models.PROTECT,
        related_name="correction_original_lines", null=True, blank=True,
    )
    proposed_number = models.ForeignKey(
        IssueNumber, on_delete=models.PROTECT,
        related_name="correction_proposed_lines", null=True, blank=True,
    )
    proposed_label = models.CharField(
        "拟议封面标识", max_length=40, blank=True,
    )
    seq = models.PositiveIntegerField("行序", default=0)

    class Meta:
        ordering = ["seq", "id"]


class CorrectionEvent(models.Model):
    """更正单状态机审计：创建、修改、应用、撤回（含幂等重放/乱序拒绝）。"""

    class Action(models.TextChoices):
        CREATE = "create", "创建草稿"
        UPDATE = "update", "修改草稿"
        APPLY = "apply", "应用"
        REAPPLY = "reapply", "重复应用（幂等）"
        WITHDRAW = "withdraw", "撤回"
        REWITHDRAW = "rewithdraw", "重复撤回（幂等）"
        REJECT = "reject", "乱序/冲突拒绝"

    correction = models.ForeignKey(
        IssueCorrection, on_delete=models.CASCADE, related_name="events",
    )
    action = models.CharField(max_length=12, choices=Action.choices)
    detail = models.CharField("说明", max_length=255, blank=True)
    expected_version = models.PositiveIntegerField(null=True, blank=True)
    issue_version_after = models.PositiveIntegerField(null=True, blank=True)
    actor = models.CharField("操作人", max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class ExportRecord(models.Model):
    """导出/书目上报记录：导出瞬间冻结覆盖关系快照与当时的版本号。

    之后无论更正应用还是撤回，快照内容不再改变——旧导出永远能追溯到
    它导出时那个版本的投影（历史装订记录与定位证据同理）。
    """

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="export_records",
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT, related_name="export_records",
    )
    coverage_version = models.PositiveIntegerField("导出时覆盖版本")
    correction = models.ForeignKey(
        IssueCorrection, on_delete=models.SET_NULL,
        related_name="export_records", null=True, blank=True,
        help_text="导出时生效的更正单（无则为原始发行关系）",
    )
    snapshot = models.JSONField("覆盖关系快照", default=dict)
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "导出记录"
        ordering = ["-created_at", "id"]


# ---------- 更正领域服务 ----------

def build_coverage_snapshot(issue):
    """冻结一个 Issue 当前的覆盖投影：期号行 + 发行区间 + 版本号。"""
    return {
        "issue_id": issue.id,
        "kind": issue.kind,
        "issue_month": issue.issue_month.isoformat() if issue.issue_month else None,
        "issue_month_end": (
            issue.issue_month_end.isoformat() if issue.issue_month_end else None
        ),
        "coverage_version": issue.coverage_version,
        "numbers": [
            {
                "number_id": nn.number_id,
                "volume": nn.number.volume,
                "number": nn.number.number,
                "label": nn.label,
            }
            for nn in IssueNumbering.objects.filter(issue=issue)
            .select_related("number").order_by("id")
        ],
    }


def latest_applied_correction(issue):
    """该发行当前生效的更正单（已应用且未撤回）；没有则 None。"""
    return (
        IssueCorrection.objects.filter(issue=issue, status=IssueCorrection.Status.APPLIED)
        .order_by("-applied_at", "-id")
        .first()
    )


def pending_correction(issue):
    """阻断后续入藏/装订的草稿更正单。"""
    return (
        IssueCorrection.objects.filter(issue=issue, status=IssueCorrection.Status.DRAFT)
        .first()
    )


def assert_no_pending_correction(issue, action="入藏或装订"):
    corr = pending_correction(issue)
    if corr is not None:
        raise CorrectionConflict(
            f"该发行期存在未决更正单 #{corr.id}（{corr.reason}），"
            f"请先应用或撤回后再{action}。",
            code="pending_correction",
            conflicts=[{"correction_id": corr.id, "reason": corr.reason}],
        )


def validate_proposed_coverage(issue, proposed_number_ids, start, end):
    """整张更正单的覆盖校验（合刊多行作为一个整体）。

    返回去重后的拟议编号列表。任何一条不合法都不允许应用/落单，
    避免「静默把编号挪给别的发行期」。
    """
    if not proposed_number_ids:
        raise CorrectionConflict(
            "更正后至少保留一个覆盖期号；如整期作废请走停刊/注销流程。",
            code="empty_coverage",
        )
    if len(set(proposed_number_ids)) != len(proposed_number_ids):
        raise CorrectionConflict("拟议期号不能重复。", code="duplicate_number")
    numbers = list(
        IssueNumber.objects.filter(id__in=set(proposed_number_ids))
    )
    if len(numbers) != len(set(proposed_number_ids)):
        raise CorrectionConflict("拟议期号中有未登记的编号。", code="unknown_number")
    wrong = [n.id for n in numbers if n.title_id != issue.title_id]
    if wrong:
        raise CorrectionConflict(
            f"期号 {wrong} 不属于该刊。", code="wrong_title",
        )
    # 一个编号槽位只能被一次发行覆盖：被其他 Issue 占用即整单拒绝
    occupied = list(
        IssueNumbering.objects.filter(number__in=numbers)
        .exclude(issue_id=issue.id)
        .values_list("number__volume", "number__number", "issue_id")
    )
    if occupied:
        raise CorrectionConflict(
            f"拟议期号已被其他发行期占用：{occupied}；整张更正单被拒绝。",
            code="number_occupied",
            conflicts=[{"volume": v, "number": n, "issue_id": i}
                       for v, n, i in occupied],
        )
    # 合刊/普通期的形态约束保持不变
    if issue.kind == Issue.IssueKind.COMBINED and len(numbers) < 2:
        raise CorrectionConflict("合刊更正后仍须覆盖至少两个期号。", code="combined_shrunk")
    if issue.kind == Issue.IssueKind.REGULAR and len(numbers) != 1:
        raise CorrectionConflict("普通期更正后只能覆盖一个期号。", code="regular_expanded")
    if end and start and end < start:
        raise CorrectionConflict("拟议截止年月不能早于起始年月。", code="bad_range")
    return numbers


def _log_event(correction, action, detail="", expected_version=None,
               issue_version_after=None):
    return CorrectionEvent.objects.create(
        correction=correction, action=action, detail=detail,
        expected_version=expected_version,
        issue_version_after=issue_version_after,
    )


@transaction.atomic()
def apply_correction(correction, expected_version=None, actor=""):
    """应用更正单（幂等）。

    重复应用：不再改变任何关系，只记 REAPPLY 审计；
    乱序（expected_version 与现行版本不符）：抛 409 且不改数据；
    已撤回/编号此时被占：整单拒绝，编号关系维持原状。
    """
    corr = IssueCorrection.objects.select_for_update().get(pk=correction.pk)
    issue = Issue.objects.select_for_update().get(pk=corr.issue_id)

    if expected_version is not None and expected_version != corr.version:
        _log_event(
            corr, CorrectionEvent.Action.REJECT,
            f"乱序应用：客户端版本 {expected_version}，现行版本 {corr.version}",
            expected_version, issue.coverage_version,
        )
        raise CorrectionConflict(
            f"更正单已过期：客户端版本 {expected_version}，现行版本 {corr.version}，"
            "请刷新后重试。",
            code="stale_version",
        )

    if corr.status == IssueCorrection.Status.APPLIED:
        _log_event(
            corr, CorrectionEvent.Action.REAPPLY,
            "重复应用，关系不变", expected_version, issue.coverage_version,
        )
        return corr, False

    if corr.status == IssueCorrection.Status.WITHDRAWN:
        raise CorrectionConflict(
            f"更正单 #{corr.id} 已撤回，不能应用；请新建更正单。",
            code="already_withdrawn",
        )

    lines = list(corr.lines.all())
    proposed_ids = [
        ln.proposed_number_id for ln in lines if ln.proposed_number_id is not None
    ]
    # 应用前再校验一次（草稿期间可能已有新发行占用了拟议编号）
    validate_proposed_coverage(
        issue, proposed_ids,
        corr.proposed_issue_month, corr.proposed_issue_month_end,
    )

    # 改写现行投影：先删后建，全部在同一事务里，失败不留半条关联
    IssueNumbering.objects.filter(issue=issue).delete()
    IssueNumbering.objects.bulk_create([
        IssueNumbering(
            issue=issue, number_id=ln.proposed_number_id,
            label=ln.proposed_label or "",
        )
        for ln in lines if ln.proposed_number_id is not None
    ])
    issue.issue_month = corr.proposed_issue_month
    issue.issue_month_end = corr.proposed_issue_month_end
    issue.coverage_version += 1
    issue.save(update_fields=[
        "issue_month", "issue_month_end", "coverage_version",
    ])

    corr.status = IssueCorrection.Status.APPLIED
    corr.version += 1
    corr.applied_at = timezone.now()
    corr.applied_issue_version = issue.coverage_version
    corr.save(update_fields=[
        "status", "version", "applied_at", "applied_issue_version", "updated_at",
    ])
    _log_event(
        corr, CorrectionEvent.Action.APPLY,
        f"应用后覆盖版本 v{issue.coverage_version}",
        expected_version, issue.coverage_version,
    )
    return corr, True


@transaction.atomic()
def withdraw_correction(correction, expected_version=None, actor=""):
    """撤回更正单（仅最新生效的那张可撤回），凭 original_* 快照还原。

    重复撤回幂等；撤回时若原编号已被其他发行期占用（更正后又入藏新刊），
    整单拒绝，现行投影保持不动。
    """
    corr = IssueCorrection.objects.select_for_update().get(pk=correction.pk)
    issue = Issue.objects.select_for_update().get(pk=corr.issue_id)

    if expected_version is not None and expected_version != corr.version:
        _log_event(
            corr, CorrectionEvent.Action.REJECT,
            f"乱序撤回：客户端版本 {expected_version}，现行版本 {corr.version}",
            expected_version, issue.coverage_version,
        )
        raise CorrectionConflict(
            f"更正单已过期：客户端版本 {expected_version}，现行版本 {corr.version}，"
            "请刷新后重试。",
            code="stale_version",
        )

    if corr.status == IssueCorrection.Status.WITHDRAWN:
        _log_event(
            corr, CorrectionEvent.Action.REWITHDRAW,
            "重复撤回，关系不变", expected_version, issue.coverage_version,
        )
        return corr, False

    if corr.status == IssueCorrection.Status.DRAFT:
        raise CorrectionConflict(
            "草稿更正单不能撤回；请直接修改或删除。", code="draft_withdraw",
        )

    # 只允许撤回最后生效的更正，防止乱序还原
    later = IssueCorrection.objects.filter(
        issue=issue, status=IssueCorrection.Status.APPLIED,
    ).exclude(pk=corr.pk).exists()
    if later:
        raise CorrectionConflict(
            f"更正单 #{corr.id} 之后还有更新的已应用更正，"
            "请先撤回最新的更正单。",
            code="not_latest",
        )

    original_ids = [row["number_id"] for row in (corr.original_numbers or [])]
    validate_proposed_coverage(
        issue, original_ids,
        corr.original_issue_month, corr.original_issue_month_end,
    )

    IssueNumbering.objects.filter(issue=issue).delete()
    IssueNumbering.objects.bulk_create([
        IssueNumbering(
            issue=issue, number_id=row["number_id"], label=row.get("label", ""),
        )
        for row in (corr.original_numbers or [])
    ])
    issue.issue_month = corr.original_issue_month
    issue.issue_month_end = corr.original_issue_month_end
    issue.coverage_version += 1
    issue.save(update_fields=[
        "issue_month", "issue_month_end", "coverage_version",
    ])

    corr.status = IssueCorrection.Status.WITHDRAWN
    corr.version += 1
    corr.withdrawn_at = timezone.now()
    corr.withdrawn_issue_version = issue.coverage_version
    corr.save(update_fields=[
        "status", "version", "withdrawn_at", "withdrawn_issue_version",
        "updated_at",
    ])
    _log_event(
        corr, CorrectionEvent.Action.WITHDRAW,
        f"撤回还原至覆盖版本 v{issue.coverage_version}",
        expected_version, issue.coverage_version,
    )
    return corr, True
