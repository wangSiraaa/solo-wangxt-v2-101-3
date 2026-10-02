"""发行更正单验收：

1) 合刊覆盖编号与跨月区间更正后，两期均可定位到原实体，时间轴显示新范围；
2) 冲突编号属于另一发行期时整张更正单被拒绝，不留下半条关联；
3) 重复应用/撤回/乱序操作幂等，历史快照仍显示应用前内容；
4) 更正后装订、拆订并刷新，位置与多期号关系保持一致，旧导出追溯原版本。
"""
import pytest
from rest_framework.test import APIClient

from serials.models import (
    CorrectionEvent, CorrectionNumberSnapshot, CorrectionOrder,
    ExportSnapshot, Issue, IssueNumber, IssueNumbering, Item,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def combined_title(db):
    """《双月刊》：v.8 no.3-4 两期合刊，一个实物 CB-34；no.5 空闲。"""
    from serials.models import Title
    t = Title.objects.create(title="合刊更正报", issn="7777-8888")
    n3 = IssueNumber.objects.create(title=t, volume="8", number="3", sort_key=3)
    n4 = IssueNumber.objects.create(title=t, volume="8", number="4", sort_key=4)
    n5 = IssueNumber.objects.create(title=t, volume="8", number="5", sort_key=5)
    n6 = IssueNumber.objects.create(title=t, volume="8", number="6", sort_key=6)
    comb = Issue.objects.create(
        title=t, kind=Issue.IssueKind.COMBINED,
        issue_month="2024-03-01", issue_month_end="2024-04-01",
        note="原 3-4 月合刊",
    )
    comb.numbers.set([n3, n4])
    IssueNumbering.objects.filter(issue=comb).update(label="no.3-4")
    item = Item.objects.create(
        barcode="CB-34", title=t, issue=comb, location="期刊区B-02",
    )
    return t, {"n3": n3, "n4": n4, "n5": n5, "n6": n6}, item, comb


def _draft(api, t, lines, reason="编辑部更正封面", ref=""):
    payload = {"title": t.id, "reason": reason, "lines": lines}
    if ref:
        payload["client_ref"] = ref
    return api.post("/api/corrections/", payload, format="json")


def _numbers_of(issue_id):
    return sorted(
        IssueNumbering.objects.filter(issue_id=issue_id)
        .values_list("number__number", flat=True),
    )


# ---------- 验收 1：合刊覆盖编号 + 跨月区间更正 ----------

@pytest.mark.django_db
def test_combined_correction_relocates_both_numbers(api, combined_title):
    t, nums, item, comb = combined_title
    n3, n4, n5 = nums["n3"], nums["n4"], nums["n5"]

    resp = _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01", "issue_month_end": "2024-03-01",
        "number_ids": [n4.id, n5.id],
    }], ref="corr-001")
    assert resp.status_code == 201, resp.json()
    order_id = resp.json()["id"]
    assert resp.json()["status"] == "draft"

    resp = api.post(f"/api/corrections/{order_id}/apply/", {}, format="json")
    assert resp.status_code == 200, resp.json()
    assert resp.json()["changed"] is True

    comb.refresh_from_db()
    # 实体没换：条码仍是 CB-34；投影换了：覆盖 n4+n5，区间跨 2~3 月
    assert str(comb.issue_month) == "2024-02-01"
    assert str(comb.issue_month_end) == "2024-03-01"
    assert _numbers_of(comb.id) == ["4", "5"]
    assert Item.objects.get(barcode="CB-34").issue_id == comb.id

    # 两个新期号都能定位到原实物
    for num in ("4", "5"):
        m = api.get(f"/api/items/locate/?title={t.id}&volume=8&number={num}").json()
        assert m["holding_status"] == "issued+held"
        assert [x["barcode"] for x in m["matches"]] == ["CB-34"], num
    # 旧期号 no.3 已无发行投影 → 缺号（历史仍在快照里）
    body = api.get(f"/api/items/locate/?title={t.id}&volume=8&number=3").json()
    assert body["holding_status"] == "not_published"
    assert body["matches"] == []

    # 条码反查使用更正后的投影
    m = api.get("/api/items/locate/?barcode=CB-34").json()["matches"][0]
    assert {x["number"] for x in m["numbers"]} == {"4", "5"}
    assert m["issue_version"] == 2

    # 时间轴显示新范围与更正标记；旧 no.3 槽位空
    tl = api.get(f"/api/timeline/?title={t.id}").json()
    slots = {s["number"]: s for s in tl["slots"]}
    assert slots["3"]["issues"] == []
    # 空槽位仍可追溯：no.3 更正前曾被本期覆盖
    formerly = slots["3"]["formerly_issued"]
    assert len(formerly) == 1
    assert formerly[0]["order_id"] == order_id
    assert formerly[0]["reassigned_to"] == ["4", "5"]
    iss_box = slots["4"]["issues"][0]
    assert iss_box["issue_month"] == "2024-02-01"
    assert iss_box["issue_month_end"] == "2024-03-01"
    assert iss_box["version"] == 2
    cur = iss_box["current_correction"]
    assert cur["order_id"] == order_id
    assert [x["number"] for x in cur["before"]["numbers"]] == ["3", "4"]
    assert [x["number"] for x in cur["after"]["numbers"]] == ["4", "5"]
    assert str(cur["before"]["issue_month_end"]) == "2024-04-01"


# ---------- 验收 2：冲突编号 → 整张拒绝，无半条关联 ----------

@pytest.mark.django_db
def test_cover_label_is_snapshotted_and_replayed(api, combined_title):
    t, nums, item, comb = combined_title
    n4, n5 = nums["n4"], nums["n5"]
    _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01", "issue_month_end": "2024-03-01",
        "number_ids": [n4.id, n5.id],
        "labels": {str(n4.id): "no.4-5", str(n5.id): "no.4-5"},
    }])
    oid = CorrectionOrder.objects.get().id
    api.post(f"/api/corrections/{oid}/apply/", {}, format="json")
    assert set(IssueNumbering.objects.filter(issue=comb).values_list(
        "number__number", "label")) == {("4", "no.4-5"), ("5", "no.4-5")}
    # 撤回后恢复原标签 no.3-4
    api.post(f"/api/corrections/{oid}/withdraw/", {}, format="json")
    assert set(IssueNumbering.objects.filter(issue=comb).values_list(
        "number__number", "label")) == {
        ("3", "no.3-4"), ("4", "no.3-4"),
    }


@pytest.mark.django_db
def test_draft_rejected_when_number_belongs_to_foreign_issue(api, combined_title):
    t, nums, item, comb = combined_title
    n5 = nums["n5"]
    # no.5 已被另一期占用
    other = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    other.numbers.add(n5)

    before = list(IssueNumbering.objects.values_list("issue_id", "number_id"))
    resp = _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01",
        "number_ids": [nums["n4"].id, n5.id],
    }])
    assert resp.status_code == 400
    assert "整张更正单已拒绝" in resp.json()["detail"]
    # 连更正单都没有留下
    assert CorrectionOrder.objects.count() == 0
    # 关联表一行未动
    assert list(IssueNumbering.objects.values_list("issue_id", "number_id")) == before


@pytest.mark.django_db
def test_apply_conflict_rolls_back_all_combined_lines(api, combined_title):
    """开单时空闲，应用前编号被另一发行期占用 → 409，合刊多行不允许半张提交。"""
    t, nums, item, comb = combined_title
    n5, n6 = nums["n5"], nums["n6"]
    resp = _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01", "issue_month_end": "2024-03-01",
        "number_ids": [n5.id, n6.id],
    }])
    assert resp.status_code == 201
    order_id = resp.json()["id"]

    # 乱序到达：另一期先占用了 no.5
    intruder = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    intruder.numbers.add(n5)

    before_rows = set(IssueNumbering.objects.values_list("issue_id", "number_id"))
    resp = api.post(f"/api/corrections/{order_id}/apply/", {}, format="json")
    assert resp.status_code == 409
    assert resp.json()["code"] == "number_occupied"
    # 没有半条关联：合刊仍完整挂 n3+n4，入侵者仍挂 n5
    assert set(IssueNumbering.objects.values_list("issue_id", "number_id")) == before_rows
    assert _numbers_of(comb.id) == ["3", "4"]
    comb.refresh_from_db()
    assert str(comb.issue_month) == "2024-03-01"  # 月份也没改
    # 单子仍是草稿，审计里没有成功应用
    assert CorrectionOrder.objects.get(id=order_id).status == "draft"


@pytest.mark.django_db
def test_swap_numbers_between_two_issues_is_one_transaction(api, combined_title):
    """合刊让出 no.3、普通期让出 no.5：两期在同一张单内互换，整体成功。"""
    t, nums, item, comb = combined_title
    n3, n4, n5 = nums["n3"], nums["n4"], nums["n5"]
    reg5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    reg5.numbers.add(n5)
    resp = _draft(api, t, [
        {"issue": comb.id, "kind": "combined",
         "issue_month": "2024-04-01", "issue_month_end": "2024-05-01",
         "number_ids": [n4.id, n5.id]},
        {"issue": reg5.id, "kind": "regular",
         "issue_month": "2024-03-01",
         "number_ids": [n3.id]},
    ], reason="期号互换")
    assert resp.status_code == 201, resp.json()
    oid = resp.json()["id"]
    resp = api.post(f"/api/corrections/{oid}/apply/", {}, format="json")
    assert resp.status_code == 200, resp.json()
    assert _numbers_of(comb.id) == ["4", "5"]
    assert _numbers_of(reg5.id) == ["3"]
    # 两条行版本都 +1
    body = api.get(f"/api/corrections/{oid}/").json()
    assert {l["issue_id"]: l["version_after"] for l in body["lines"]} == {
        comb.id: 2, reg5.id: 2,
    }


# ---------- 验收 3：幂等 + 乱序 + 历史快照 ----------

@pytest.mark.django_db
def test_repeated_apply_and_withdraw_are_idempotent(api, combined_title):
    t, nums, item, comb = combined_title
    n4, n5 = nums["n4"], nums["n5"]
    oid = _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01", "issue_month_end": "2024-03-01",
        "number_ids": [n4.id, n5.id],
    }], ref="corr-002").json()["id"]

    r1 = api.post(f"/api/corrections/{oid}/apply/", {}, format="json")
    assert r1.json()["changed"] is True
    r2 = api.post(f"/api/corrections/{oid}/apply/", {}, format="json")
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    # 重复应用不重复改变关系/版本
    comb.refresh_from_db()
    assert comb.version == 2
    assert _numbers_of(comb.id) == ["4", "5"]
    actions = list(
        CorrectionEvent.objects.filter(order_id=oid)
        .values_list("action", flat=True)
    )
    assert actions.count("apply") == 1
    assert actions.count("apply_replay") == 1

    # 撤回 → 反向重放到更正前
    w1 = api.post(f"/api/corrections/{oid}/withdraw/", {}, format="json")
    assert w1.status_code == 200
    assert w1.json()["changed"] is True
    comb.refresh_from_db()
    assert comb.version == 3
    assert _numbers_of(comb.id) == ["3", "4"]
    assert str(comb.issue_month) == "2024-03-01"
    assert str(comb.issue_month_end) == "2024-04-01"

    # 撤回重放幂等
    w2 = api.post(f"/api/corrections/{oid}/withdraw/", {}, format="json")
    assert w2.status_code == 200
    assert w2.json()["changed"] is False
    comb.refresh_from_db()
    assert comb.version == 3

    # 历史快照仍完整显示应用前内容
    body = api.get(f"/api/corrections/{oid}/").json()
    line = body["lines"][0]
    assert [x["number"] for x in line["before"]["numbers"]] == ["3", "4"]
    assert [x["number"] for x in line["after"]["numbers"]] == ["4", "5"]
    assert line["version_before"] == 1
    assert line["version_after"] == 2
    # 快照去规范化：直接读库也能还原原关系
    snaps = CorrectionNumberSnapshot.objects.filter(
        line__order_id=oid, side="before",
    ).values_list("volume", "number_label")
    assert sorted(snaps) == [("8", "3"), ("8", "4")]


@pytest.mark.django_db
def test_withdraw_after_newer_correction_is_rejected(api, combined_title):
    """乱序：A 单应用后 B 单又改过同一期，撤回 A 必须 409 且不还原。"""
    t, nums, item, comb = combined_title
    n4, n5, n6 = nums["n4"], nums["n5"], nums["n6"]

    def make(ids):
        return _draft(api, t, [{
            "issue": comb.id, "kind": "combined",
            "issue_month": "2024-02-01", "issue_month_end": "2024-03-01",
            "number_ids": ids,
        }]).json()["id"]

    oid_a = make([n4.id, n5.id])
    api.post(f"/api/corrections/{oid_a}/apply/", {}, format="json")
    # B 单基于 A 的投影继续更正
    oid_b = make([n5.id, n6.id])
    rb = api.post(f"/api/corrections/{oid_b}/apply/", {}, format="json")
    assert rb.status_code == 200

    r = api.post(f"/api/corrections/{oid_a}/withdraw/", {}, format="json")
    assert r.status_code == 409
    assert r.json()["code"] == "superseded"
    # 投影保持 B 应用后的状态
    assert _numbers_of(comb.id) == ["5", "6"]
    assert CorrectionOrder.objects.get(id=oid_a).status == "applied"


@pytest.mark.django_db
def test_client_ref_makes_draft_submission_idempotent(api, combined_title):
    t, nums, item, comb = combined_title
    n4, n5 = nums["n4"], nums["n5"]
    payload = {
        "title": t.id, "reason": "r", "client_ref": "same-ref",
        "lines": [{
            "issue": comb.id, "kind": "combined",
            "issue_month": "2024-02-01",
            "number_ids": [n4.id, n5.id],
        }],
    }
    r1 = api.post("/api/corrections/", payload, format="json")
    r2 = api.post("/api/corrections/", payload, format="json")
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert CorrectionOrder.objects.count() == 1


# ---------- 后续入藏/装订冲突提示 ----------

@pytest.mark.django_db
def test_accession_blocked_while_pending_draft(api, combined_title):
    t, nums, item, comb = combined_title
    n4, n5 = nums["n4"], nums["n5"]
    _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01",
        "number_ids": [n4.id, n5.id],
    }])
    # 挂草稿期间给该合刊补实物 → 409 明确冲突提示
    resp = api.post("/api/items/", {
        "barcode": "CB-34X", "title": t.id,
        "issue": comb.id, "location": "期刊区B-03",
    }, format="json")
    assert resp.status_code == 409
    assert resp.json()["code"] == "pending_correction"
    # 实物确实没建成
    assert not Item.objects.filter(barcode="CB-34X").exists()

    # 草稿撤回后入藏恢复正常
    oid = CorrectionOrder.objects.get(status="draft").id
    api.post(f"/api/corrections/{oid}/withdraw/", {}, format="json")
    resp = api.post("/api/items/", {
        "barcode": "CB-34X", "title": t.id,
        "issue": comb.id, "location": "期刊区B-03",
    }, format="json")
    assert resp.status_code == 201


@pytest.mark.django_db
def test_binding_blocked_while_pending_draft(api, combined_title):
    t, nums, item, comb = combined_title
    n4, n5 = nums["n4"], nums["n5"]
    _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01",
        "number_ids": [n4.id, n5.id],
    }])
    resp = api.post("/api/bindings/", {
        "call_number": "Q/PEND", "title": t.id,
        "location": "装订库 P-1", "item_ids": [item.id],
    }, format="json")
    assert resp.status_code == 409
    assert resp.json()["code"] == "pending_correction"
    tl = api.get(f"/api/timeline/?title={t.id}").json()
    box = tl["slots"][0]["issues"][0]
    # 时间轴明确提示有待决更正
    assert box["has_pending_correction"] is True

    # 应用后再装订即放行
    oid = CorrectionOrder.objects.get(status="draft").id
    api.post(f"/api/corrections/{oid}/apply/", {}, format="json")
    resp = api.post("/api/bindings/", {
        "call_number": "Q/OK", "title": t.id,
        "location": "装订库 P-2", "item_ids": [item.id],
    }, format="json")
    assert resp.status_code == 201, resp.json()


# ---------- 验收 4：更正后装订/拆订 + 旧导出追溯 ----------

@pytest.mark.django_db
def test_bind_unbind_after_correction_and_old_export_trace(api, combined_title):
    t, nums, item, comb = combined_title
    n4, n5 = nums["n4"], nums["n5"]

    # 1) 更正前导出：冻结 v1（no.3-4，2024-03~04）
    r = api.post("/api/exports/freeze/", {"barcode": "CB-34"}, format="json")
    assert r.status_code == 201
    old_export_id = r.json()["id"]
    assert r.json()["issue_version"] == 1
    assert [x["number"] for x in r.json()["numbers_json"]] == ["3", "4"]

    # 2) 更正为 no.4-5、2024-02~03
    oid = _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01", "issue_month_end": "2024-03-01",
        "number_ids": [n4.id, n5.id],
    }]).json()["id"]
    api.post(f"/api/corrections/{oid}/apply/", {}, format="json")

    # 3) 更正后装订：从任一新期号定位都指向装订册
    rb = api.post("/api/bindings/", {
        "call_number": "Q/AFTER", "title": t.id,
        "location": "装订库 C-20", "bound_month": "2024-09-01",
        "item_ids": [item.id],
    }, format="json")
    assert rb.status_code == 201
    for num in ("4", "5"):
        m = api.get(
            f"/api/items/locate/?title={t.id}&volume=8&number={num}",
        ).json()["matches"][0]
        assert m["barcode"] == "CB-34"
        assert m["bound"] is True
        assert m["binding"] == "Q/AFTER"
        assert m["location"] == "装订库 C-20"

    # 4) 拆订并刷新：实际位置恢复，多期号关系仍是更正后的 4-5
    ru = api.post("/api/bindings/unbind/",
                  {"binding_id": rb.json()["id"]}, format="json")
    assert ru.status_code == 200
    item.refresh_from_db()
    assert item.current_location() == "期刊区B-02"
    assert _numbers_of(comb.id) == ["4", "5"]
    for num in ("4", "5"):
        m = api.get(
            f"/api/items/locate/?title={t.id}&volume=8&number={num}",
        ).json()["matches"][0]
        assert m["bound"] is False
        assert m["location"] == "期刊区B-02"

    # 5) 旧导出记录仍追溯到更正前版本
    old = api.get(f"/api/exports/{old_export_id}/").json()
    assert old["issue_version"] == 1
    assert [x["number"] for x in old["numbers_json"]] == ["3", "4"]
    assert old["month_start"] == "2024-03-01"
    assert old["month_end"] == "2024-04-01"
    assert old["barcode"] == "CB-34"
    # 新导出则是 v2 的更正后投影
    r2 = api.post("/api/exports/freeze/", {"barcode": "CB-34"}, format="json")
    assert r2.json()["issue_version"] == 2
    assert [x["number"] for x in r2.json()["numbers_json"]] == ["4", "5"]
    # 两条导出快照都在
    assert ExportSnapshot.objects.count() == 2


# ---------- 草稿撤回 / 删除等状态规则 ----------

@pytest.mark.django_db
def test_draft_withdraw_and_applied_cannot_be_deleted(api, combined_title):
    t, nums, item, comb = combined_title
    n4, n5 = nums["n4"], nums["n5"]
    oid = _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01",
        "number_ids": [n4.id, n5.id],
    }]).json()["id"]

    # 草稿可撤回（不动投影）
    r = api.post(f"/api/corrections/{oid}/withdraw/", {}, format="json")
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert _numbers_of(comb.id) == ["3", "4"]
    # 已撤回不能再应用
    r2 = api.post(f"/api/corrections/{oid}/apply/", {}, format="json")
    assert r2.status_code == 409

    # 已应用的单子不能删除，只能撤回
    oid2 = _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-01-01",
        "number_ids": [n4.id, n5.id],
    }]).json()["id"]
    api.post(f"/api/corrections/{oid2}/apply/", {}, format="json")
    rd = api.delete(f"/api/corrections/{oid2}/")
    assert rd.status_code == 409


@pytest.mark.django_db
def test_withdraw_rejected_when_number_since_claimed(api, combined_title):
    """应用后释放出的编号若被新发行期占用，撤回不能静默抢回。"""
    t, nums, item, comb = combined_title
    n3, n4, n5 = nums["n3"], nums["n4"], nums["n5"]
    oid = _draft(api, t, [{
        "issue": comb.id, "kind": "combined",
        "issue_month": "2024-02-01",
        "number_ids": [n4.id, n5.id],
    }]).json()["id"]
    api.post(f"/api/corrections/{oid}/apply/", {}, format="json")
    # no.3 被更正释放后，又登记了新的发行期
    new_iss = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-06-01",
    )
    new_iss.numbers.add(n3)

    r = api.post(f"/api/corrections/{oid}/withdraw/", {}, format="json")
    assert r.status_code == 409
    assert r.json()["code"] == "number_occupied"
    assert _numbers_of(comb.id) == ["4", "5"]
    assert _numbers_of(new_iss.id) == ["3"]
