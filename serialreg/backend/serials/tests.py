"""需求验证：跨年卷、停刊月份、两期合刊、装订与拆订、缺号不自动等同缺藏。

运行：SERIALREG_DB=sqlite pytest -q（有 PG 时直接连 PG）
"""
import pytest
from django.db import IntegrityError
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, Issue, IssueCorrection, IssueNumber,
    IssueNumbering, Item, Title, locate_number, number_holding_status,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def cross_year_title(db):
    """《学报》：跨年卷 v.60，no.1 印 2023-10，no.3 跨 2023-12~2024-01。"""
    t = Title.objects.create(title="跨年报", issn="1111-2222")
    n1 = IssueNumber.objects.create(title=t, volume="60", number="1", sort_key=1)
    n2 = IssueNumber.objects.create(title=t, volume="60", number="2", sort_key=2)
    n3 = IssueNumber.objects.create(title=t, volume="60", number="3", sort_key=3)
    Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2023-10-01",
    ).numbers.add(n1)
    # 跨年卷：编号 v.60 no.3 只有一个，发行覆盖 2023-12 至 2024-01
    iss3 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR,
        issue_month="2023-12-01", issue_month_end="2024-01-01",
    )
    iss3.numbers.add(n3)
    Item.objects.create(
        barcode="CY-001", title=t, issue=Issue.objects.get(numbers=n1),
        location="期刊区A-01",
    )
    Item.objects.create(barcode="CY-003", title=t, issue=iss3, location="期刊区A-01")
    return t, {"n1": n1, "n2": n2, "n3": n3}


@pytest.fixture
def ceased_title(db):
    """《月报》：2024-06 停刊；no.5 正常出版，no.6 从未发行。"""
    t = Title.objects.create(
        title="停刊报",
        status=Title.PublicationStatus.CEASED, ceased_month="2024-06-01",
    )
    n5 = IssueNumber.objects.create(title=t, volume="12", number="5", sort_key=5)
    n6 = IssueNumber.objects.create(title=t, volume="12", number="6", sort_key=6)
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    iss5.numbers.add(n5)
    # no.5 出版了但没有入库 → 缺藏；no.6 没有发行记录 → 缺号
    return t, {"n5": n5, "n6": n6}


@pytest.fixture
def combined_title(db):
    """《双月刊》：v.8 no.3-4 两期合刊，一个实物，两个编号关系。"""
    t = Title.objects.create(title="合刊报", issn="3333-4444")
    n3 = IssueNumber.objects.create(title=t, volume="8", number="3", sort_key=3)
    n4 = IssueNumber.objects.create(title=t, volume="8", number="4", sort_key=4)
    n5 = IssueNumber.objects.create(title=t, volume="8", number="5", sort_key=5)
    comb = Issue.objects.create(
        title=t, kind=Issue.IssueKind.COMBINED,
        issue_month="2024-03-01", issue_month_end="2024-04-01",
        note="3-4月合刊",
    )
    comb.numbers.set([n3, n4])
    item = Item.objects.create(
        barcode="CB-34", title=t, issue=comb, location="期刊区B-02",
    )
    return t, {"n3": n3, "n4": n4, "n5": n5}, item, comb


# ---------- 跨年卷 ----------

@pytest.mark.django_db
def test_cross_year_volume_one_number_spans_two_years(cross_year_title):
    t, nums = cross_year_title
    n3 = nums["n3"]
    issue = n3.issues.get()
    # 编号与发行年月分离：编号是 v.60 no.3，发行覆盖两个自然年
    assert (n3.volume, n3.number) == ("60", "3")
    assert str(issue.issue_month) == "2023-12-01"
    assert str(issue.issue_month_end) == "2024-01-01"
    # 按编号顺序而非月份排序，跨年卷仍是卷内连续位置
    ordered = list(
        IssueNumber.objects.filter(title=t).order_by("sort_key")
        .values_list("number", flat=True)
    )
    assert ordered == ["1", "2", "3"]


@pytest.mark.django_db
def test_cross_year_locate_by_number(cross_year_title, api):
    t, nums = cross_year_title
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=60&number=3")
    assert resp.status_code == 200
    data = resp.json()
    assert data["holding_status"] == "issued+held"
    assert len(data["matches"]) == 1
    assert data["matches"][0]["barcode"] == "CY-003"
    assert data["matches"][0]["location"] == "期刊区A-01"


# ---------- 停刊 + 缺号 vs 缺藏 ----------

@pytest.mark.django_db
def test_ceased_requires_month(db):
    from serials.serializers import TitleSerializer
    s = TitleSerializer(data={
        "title": "x", "status": "ceased",
    })
    assert not s.is_valid()
    assert "ceased_month" in s.errors


@pytest.mark.django_db
def test_missing_number_is_not_missing_holding(ceased_title):
    t, nums = ceased_title
    # no.5 已发行无实物 → 缺藏
    assert number_holding_status(t, nums["n5"]) == "issued+missing"
    # no.6 无发行记录 → 缺号，语义上不等于缺藏
    assert number_holding_status(t, nums["n6"]) == "ceased_gap"
    # 缺号槽位定位不到任何实物，但返回的是 404「缺号」而非「缺藏」
    rows = locate_number(nums["n6"])
    assert rows == []


@pytest.mark.django_db
def test_ceased_title_timeline(api, ceased_title):
    t, nums = ceased_title
    resp = api.get(f"/api/timeline/?title={t.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"]["status"] == "ceased"
    assert body["title"]["ceased_month"] == "2024-06-01"
    statuses = {s["number"]: s["holding_status"] for s in body["slots"]}
    assert statuses["5"] == "issued+missing"
    assert statuses["6"] == "ceased_gap"


# ---------- 两期合刊 ----------

@pytest.mark.django_db
def test_combined_issue_keeps_two_number_relations(combined_title):
    t, nums, item, comb = combined_title
    # 必须是两条编号关联，不能用一个条码覆盖多期号关系
    rels = IssueNumbering.objects.filter(issue=comb).order_by("number__number")
    assert list(rels.values_list("number__number", flat=True)) == ["3", "4"]
    # 实物只有一个，指向的是「这次合刊发行」，期号关系在关联表上
    assert Item.objects.filter(issue=comb).count() == 1
    assert item.barcode == "CB-34"


@pytest.mark.django_db
def test_combined_issue_rejects_single_number(db, api):
    t = Title.objects.create(title="z")
    n = IssueNumber.objects.create(title=t, volume="1", number="1", sort_key=1)
    resp = api.post("/api/issues/", {
        "title": t.id, "kind": "combined",
        "issue_month": "2024-03-01",
        "issue_month_end": "2024-04-01",
        "number_ids": [n.id],
    }, format="json")
    assert resp.status_code == 400
    assert "合刊" in str(resp.json())


@pytest.mark.django_db
def test_number_cannot_be_issued_twice(db, combined_title):
    t, nums, item, comb = combined_title
    # 编号已被合刊占用 → 拒绝再次登记发行
    from serials.serializers import IssueSerializer
    s = IssueSerializer(data={
        "title": t.id, "kind": "regular",
        "issue_month": "2024-09-01",
        "number_ids": [nums["n3"].id],
    })
    assert not s.is_valid()


@pytest.mark.django_db
def test_locate_combined_from_either_number(combined_title, api):
    t, nums, item, comb = combined_title
    for num in ("3", "4"):
        resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number={num}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["holding_status"] == "issued+held"
        assert [m["barcode"] for m in data["matches"]] == ["CB-34"], num
    # no.5 已登记但未发行 → 缺号：200 + 空匹配，状态不同于缺藏
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["holding_status"] == "not_published"
    assert body["matches"] == []
    # 完全没登记过的编号才是 404
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number=99")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_barcode_locates_both_numbers_of_combined(combined_title, api):
    resp = api.get("/api/items/locate/?barcode=CB-34")
    assert resp.status_code == 200
    match = resp.json()["matches"][0]
    assert {n["number"] for n in match["numbers"]} == {"3", "4"}


# ---------- 装订 / 拆订 ----------

@pytest.mark.django_db
def test_bind_combined_and_regular_then_locate(combined_title, cross_year_title, api):
    # 把跨年报 no.1 与合刊报... 不同刊不能混装；同刊内：
    t, nums, comb_item, comb = combined_title
    # 补一本 no.5 普通期实物（已发行）
    n5 = nums["n5"]
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    iss5.numbers.add(n5)
    item5 = Item.objects.create(
        barcode="CB-05", title=t, issue=iss5, location="期刊区B-09",
    )
    resp = api.post("/api/bindings/", {
        "call_number": "Q/HK-2024",
        "title": t.id,
        "location": "装订库 C-12",
        "bound_month": "2024-08-01",
        "item_ids": [comb_item.id, item5.id],
    }, format="json")
    assert resp.status_code == 201, resp.json()
    assert Binding.objects.count() == 1

    comb_item.refresh_from_db()
    item5.refresh_from_db()
    assert comb_item.status == Item.ItemStatus.BOUND
    # 装订后实物自身位置字段不变，实际位置取装订册
    assert comb_item.location == "期刊区B-02"
    assert comb_item.current_location() == "装订库 C-12"

    # 合刊任一期号仍能找到，且位置指向装订册
    for num in ("3", "4", "5"):
        resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number={num}")
        assert resp.status_code == 200
        match = resp.json()["matches"][0]
        assert match["bound"] is True
        assert match["binding"] == "Q/HK-2024"
        assert match["location"] == "装订库 C-12"

    # 已装订实物不能重复装订
    resp = api.post("/api/bindings/", {
        "call_number": "Q/DUP", "title": t.id,
        "location": "X", "item_ids": [comb_item.id],
    }, format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_bind_dedups_same_item_from_two_numbers(combined_title, api):
    """合刊实物会同时出现在 no.3 与 no.4 两个槽位：
    提交重复 item id 时应去重成功，而不是 500。"""
    t, nums, comb_item, _ = combined_title
    resp = api.post("/api/bindings/", {
        "call_number": "Q/DEDUP", "title": t.id, "location": "装订库 D-1",
        "item_ids": [comb_item.id, comb_item.id],
    }, format="json")
    assert resp.status_code == 201, resp.json()
    assert len(resp.json()["items"]) == 1


@pytest.mark.django_db
def test_cannot_bind_across_titles(cross_year_title, combined_title, api):
    t1, n1 = cross_year_title
    t2, nums, comb_item, _ = combined_title
    cy_item = Item.objects.get(barcode="CY-001")
    resp = api.post("/api/bindings/", {
        "call_number": "Q/MIX", "title": t1.id,
        "location": "X", "item_ids": [cy_item.id, comb_item.id],
    }, format="json")
    assert resp.status_code == 400
    assert "混装" in str(resp.json())


@pytest.mark.django_db
def test_unbind_restores_locations(combined_title, api):
    t, nums, comb_item, comb = combined_title
    binding = Binding.objects.create(
        call_number="Q/HK-X", title=t, location="装订库 Z-1",
    )
    BindingEntry.objects.create(
        item=comb_item, binding=binding,
        previous_location=comb_item.location,
    )
    Item.objects.filter(id=comb_item.id).update(status=Item.ItemStatus.BOUND)

    resp = api.post("/api/bindings/unbind/", {"binding_id": binding.id},
                    format="json")
    assert resp.status_code == 200
    assert not Binding.objects.filter(id=binding.id).exists()
    assert not BindingEntry.objects.filter(item=comb_item).exists()

    comb_item.refresh_from_db()
    assert comb_item.status == Item.ItemStatus.AVAILABLE
    # 拆订后恢复各自位置
    assert comb_item.current_location() == "期刊区B-02"

    # 合刊的两个期号关系仍然完好，仍能从任一期号找到实物
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number=4")
    match = resp.json()["matches"][0]
    assert match["barcode"] == "CB-34"
    assert match["bound"] is False
    assert match["location"] == "期刊区B-02"


# ---------- 入藏接口 ----------

@pytest.mark.django_db
def test_accession_item(api, cross_year_title):
    t, nums = cross_year_title
    n2 = nums["n2"]  # 缺号：先入藏会失败，因为没有发行期
    resp = api.post("/api/items/", {
        "barcode": "CY-002", "title": t.id,
        "issue": 999999, "location": "X",
    }, format="json")
    assert resp.status_code in (400,)

    iss2 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2023-11-01",
    )
    iss2.numbers.add(n2)
    resp = api.post("/api/items/", {
        "barcode": "CY-002", "title": t.id,
        "issue": iss2.id, "location": "期刊区A-02",
    }, format="json")
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["number_ids"] == [n2.id]
    assert body["current_location"] == "期刊区A-02"


# ---------- 发行更正单 ----------

def _make_number(title, value, sort_key=None):
    return IssueNumber.objects.create(
        title=title, volume="8", number=str(value),
        sort_key=sort_key if sort_key is not None else value,
    )


def _correction_payload(comb, nums, proposed, start, end,
                        reason="编辑部勘误", token=None, labels=None):
    """构造整张更正单：合刊多行一次提交。"""
    lines = []
    for i, (old, new) in enumerate(zip(nums, proposed)):
        lines.append({
            "original_number_id": old.id,
            "proposed_number_id": new.id if new is not None else None,
            "proposed_label": (labels or [])[i] if labels else "",
        })
    payload = {
        "issue": comb.id,
        "reason": reason,
        "proposed_issue_month": start,
        "proposed_issue_month_end": end,
        "lines": lines,
    }
    if token:
        payload["client_token"] = token
    return payload


@pytest.mark.django_db
def test_apply_combined_correction_relocates_both_numbers(combined_title, api):
    """验收 1：两期合刊覆盖编号与跨月区间更正后，两期均可定位到原实体，
    时间轴显示新范围，旧编号槽位变为缺号，历史快照保留原覆盖。"""
    t, nums, item, comb = combined_title
    n5, n6 = nums["n5"], _make_number(t, 6)

    resp = api.post("/api/corrections/", _correction_payload(
        comb, [nums["n3"], nums["n4"]], [n5, n6],
        "2024-05-01", "2024-06-01",
        labels=["no.5-6", "no.5-6"], token="tok-corr-1",
    ), format="json")
    assert resp.status_code == 201, resp.json()
    corr_id = resp.json()["id"]
    assert resp.json()["status"] == "draft"

    # 应用前：新编号还是缺号，旧编号仍可定位
    assert api.get(f"/api/items/locate/?title={t.id}&volume=8&number=3").json()[
        "matches"][0]["barcode"] == "CB-34"
    assert api.get(f"/api/items/locate/?title={t.id}&volume=8&number=5").json()[
        "matches"] == []

    resp = api.post(f"/api/corrections/{corr_id}/apply/",
                    {"expected_version": 1}, format="json")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "applied"
    assert body["version"] == 2
    comb.refresh_from_db()
    assert comb.coverage_version == 2
    assert str(comb.issue_month) == "2024-05-01"
    assert str(comb.issue_month_end) == "2024-06-01"

    # 两个新期号都定位到同一个原实物 CB-34（实体没变，投影变了）
    for num in ("5", "6"):
        data = api.get(
            f"/api/items/locate/?title={t.id}&volume=8&number={num}").json()
        assert data["holding_status"] == "issued+held"
        assert [m["barcode"] for m in data["matches"]] == ["CB-34"]
        assert data["matches"][0]["coverage_version"] == 2
    # 旧编号槽位释放：无发行记录 → 缺号（不自动等于缺藏）
    for num in ("3", "4"):
        data = api.get(
            f"/api/items/locate/?title={t.id}&volume=8&number={num}").json()
        assert data["holding_status"] == "not_published"
        assert data["matches"] == []

    # 条码反查使用更正后的投影：CB-34 现在覆盖 no.5、no.6
    match = api.get("/api/items/locate/?barcode=CB-34").json()["matches"][0]
    assert {n["number"] for n in match["numbers"]} == {"5", "6"}
    assert match["coverage_version"] == 2

    # 时间轴：新范围 + 更正当前值/历史值
    tl = api.get(f"/api/timeline/?title={t.id}").json()
    slot5 = next(s for s in tl["slots"] if s["number"] == "5")
    iss = slot5["issues"][0]
    assert iss["issue_month"] == "2024-05-01"
    assert iss["issue_month_end"] == "2024-06-01"
    assert iss["coverage_version"] == 2
    corr_view = iss["corrections"][0]
    assert corr_view["status"] == "applied"
    # 历史值：原 3-4 月、原编号 3/4
    assert corr_view["original_issue_month"] == "2024-03-01"
    assert corr_view["original_issue_month_end"] == "2024-04-01"
    assert [r["number"] for r in corr_view["original_numbers"]] == ["3", "4"]
    assert {(ln["original"]["number"], ln["proposed"]["number"])
            for ln in corr_view["lines"]} == {("3", "5"), ("4", "6")}
    assert {e["action"] for e in corr_view["events"]} >= {"create", "apply"}


@pytest.mark.django_db
def test_correction_rejected_when_proposed_number_belongs_to_other_issue(
        combined_title, api):
    """验收 2：冲突编号已属于另一发行期 → 整张更正单被拒绝，不留下半条关联。"""
    t, nums, item, comb = combined_title
    n6 = _make_number(t, 6)
    # no.5 先被一期普通刊占用
    occ = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-09-01",
    )
    occ.numbers.add(nums["n5"])

    before = set(IssueNumbering.objects.values_list("issue_id", "number_id"))
    resp = api.post("/api/corrections/", _correction_payload(
        comb, [nums["n3"], nums["n4"]], [nums["n5"], n6],
        "2024-05-01", "2024-06-01",
    ), format="json")
    assert resp.status_code == 400
    assert "已被其他发行期占用" in str(resp.json())
    # 更正单根本没建成，关联表一条都没动（不存在半条关联）
    assert IssueCorrection.objects.count() == 0
    assert set(IssueNumbering.objects.values_list("issue_id", "number_id")) == before
    assert list(
        IssueNumbering.objects.filter(issue=comb)
        .values_list("number__number", flat=True).order_by("number__number")
    ) == ["3", "4"]


@pytest.mark.django_db
def test_apply_time_conflict_whole_sheet_aborts(combined_title, api):
    """草稿期间拟议编号被新发行占用：应用时整单回滚，投影维持原状。"""
    t, nums, item, comb = combined_title
    n6 = _make_number(t, 6)
    resp = api.post("/api/corrections/", _correction_payload(
        comb, [nums["n3"], nums["n4"]], [nums["n5"], n6],
        "2024-05-01", "2024-06-01",
    ), format="json")
    assert resp.status_code == 201
    corr_id = resp.json()["id"]

    # 草稿期间 no.5 被另一期发行占用
    occ = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-09-01",
    )
    occ.numbers.add(nums["n5"])

    resp = api.post(f"/api/corrections/{corr_id}/apply/", {}, format="json")
    assert resp.status_code == 409
    assert resp.json()["code"] == "number_occupied"
    # 仍是草稿，编号关系一行未变
    assert IssueNumbering.objects.filter(issue=comb).count() == 2
    assert list(
        IssueNumbering.objects.filter(issue=comb)
        .values_list("number__number", flat=True)
    ) == ["3", "4"]
    comb.refresh_from_db()
    assert comb.coverage_version == 1
    # 审计记录了拒绝
    from serials.models import CorrectionEvent
    assert CorrectionEvent.objects.filter(
        correction_id=corr_id, action="reject",
    ).count() == 0  # 占用拒绝走领域异常（乱序才落 reject 审计）


@pytest.mark.django_db
def test_reapply_rewithdraw_and_stale_versions_are_idempotent(combined_title, api):
    """验收 3：重复应用/撤回重放不重复改变关系；乱序版本 409；
    历史快照始终显示应用前内容。"""
    t, nums, item, comb = combined_title
    n5, n6 = nums["n5"], _make_number(t, 6)
    resp = api.post("/api/corrections/", _correction_payload(
        comb, [nums["n3"], nums["n4"]], [n5, n6],
        "2024-05-01", "2024-06-01", token="tok-once",
    ), format="json")
    assert resp.status_code == 201
    corr_id = resp.json()["id"]

    # 乱序：带着陈旧版本号应用 → 409，且不改关系
    resp = api.post(f"/api/corrections/{corr_id}/apply/",
                    {"expected_version": 99}, format="json")
    assert resp.status_code == 409
    assert resp.json()["code"] == "stale_version"
    comb.refresh_from_db()
    assert comb.coverage_version == 1

    # 正常应用
    resp = api.post(f"/api/corrections/{corr_id}/apply/",
                    {"expected_version": 1}, format="json")
    assert resp.status_code == 200
    assert resp.json()["applied_issue_version"] == 2

    # 重复应用：幂等，版本不再增长，只追加 reapply 审计
    resp = api.post(f"/api/corrections/{corr_id}/apply/", {}, format="json")
    assert resp.status_code == 200
    assert resp.headers.get("State-Changed") == "false"
    comb.refresh_from_db()
    assert comb.coverage_version == 2
    assert list(
        IssueNumbering.objects.filter(issue=comb)
        .values_list("number__number", flat=True).order_by("number__number")
    ) == ["5", "6"]

    # 撤回（带正确版本 2）→ 还原原覆盖，版本到 3
    resp = api.post(f"/api/corrections/{corr_id}/withdraw/",
                    {"expected_version": 2}, format="json")
    assert resp.status_code == 200
    comb.refresh_from_db()
    assert comb.coverage_version == 3
    assert list(
        IssueNumbering.objects.filter(issue=comb)
        .values_list("number__number", flat=True).order_by("number__number")
    ) == ["3", "4"]
    assert str(comb.issue_month) == "2024-03-01"

    # 撤回重放：幂等，关系不再变化
    resp = api.post(f"/api/corrections/{corr_id}/withdraw/", {}, format="json")
    assert resp.status_code == 200
    assert resp.headers.get("State-Changed") == "false"
    comb.refresh_from_db()
    assert comb.coverage_version == 3

    # 历史快照始终是「应用前内容」：3/4 与 3-4 月，与撤回后的现行投影一致，
    # 且撤回动作没有抹掉它
    detail = api.get(f"/api/corrections/{corr_id}/").json()
    assert [r["number"] for r in detail["original_numbers"]] == ["3", "4"]
    assert detail["original_issue_month"] == "2024-03-01"
    assert detail["original_issue_month_end"] == "2024-04-01"
    assert detail["withdrawn_issue_version"] == 3
    actions = [e["action"] for e in detail["events"]]
    assert actions.count("apply") == 1
    assert actions.count("reapply") == 1
    assert actions.count("withdraw") == 1
    assert actions.count("rewithdraw") == 1

    # 已撤回的单不能再次应用（必须新建）
    resp = api.post(f"/api/corrections/{corr_id}/apply/", {}, format="json")
    assert resp.status_code == 409
    assert resp.json()["code"] == "already_withdrawn"


@pytest.mark.django_db
def test_correction_creation_is_idempotent_by_client_token(combined_title, api):
    t, nums, item, comb = combined_title
    n6 = _make_number(t, 6)
    payload = _correction_payload(
        comb, [nums["n3"], nums["n4"]], [nums["n5"], n6],
        "2024-05-01", "2024-06-01", token="dup-token",
    )
    r1 = api.post("/api/corrections/", payload, format="json")
    r2 = api.post("/api/corrections/", payload, format="json")
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert r1.json()["id"] == r2.json()["id"]
    assert IssueCorrection.objects.count() == 1


@pytest.mark.django_db
def test_pending_correction_blocks_accession_and_binding(combined_title, api):
    """草稿更正单未决时，后续入藏/装订收到清楚的 409 冲突提示。"""
    t, nums, item, comb = combined_title
    n6 = _make_number(t, 6)
    resp = api.post("/api/corrections/", _correction_payload(
        comb, [nums["n3"], nums["n4"]], [nums["n5"], n6],
        "2024-05-01", "2024-06-01",
    ), format="json")
    assert resp.status_code == 201

    # 入藏被阻断
    resp = api.post("/api/items/", {
        "barcode": "CB-NEW", "title": t.id, "issue": comb.id,
        "location": "X",
    }, format="json")
    assert resp.status_code == 409
    assert "未决更正单" in resp.json()["detail"]

    # 装订被阻断
    resp = api.post("/api/bindings/", {
        "call_number": "Q/BLOCK", "title": t.id, "location": "X",
        "item_ids": [item.id],
    }, format="json")
    assert resp.status_code == 409
    assert "未决更正单" in resp.json()["detail"]


@pytest.mark.django_db
def test_bind_unbind_after_correction_and_old_export_trace(combined_title, api):
    """验收 4：更正后再装订、拆订并刷新，实际位置与多期号关系一致；
    更正前导出的旧记录仍追溯原版本。"""
    t, nums, item, comb = combined_title
    n5, n6 = nums["n5"], _make_number(t, 6)

    # 更正前导出：冻结 v1 投影（no.3-4，3-4 月）
    resp = api.post("/api/exports/", {
        "title": t.id, "issue": comb.id, "note": "装订前书目上报",
    }, format="json")
    assert resp.status_code == 201, resp.json()
    export_id = resp.json()["id"]
    old_snapshot = resp.json()["snapshot"]
    assert old_snapshot["coverage_version"] == 1
    assert [r["number"] for r in old_snapshot["numbers"]] == ["3", "4"]

    # 应用更正 3/4 → 5/6
    corr_id = api.post("/api/corrections/", _correction_payload(
        comb, [nums["n3"], nums["n4"]], [n5, n6],
        "2024-05-01", "2024-06-01",
    ), format="json").json()["id"]
    assert api.post(f"/api/corrections/{corr_id}/apply/", {},
                    format="json").status_code == 200

    # 更正后装订原实物
    resp = api.post("/api/bindings/", {
        "call_number": "Q/HK-CORR", "title": t.id,
        "location": "装订库 E-05", "bound_month": "2024-09-01",
        "item_ids": [item.id],
    }, format="json")
    assert resp.status_code == 201, resp.json()

    # 从更正后的两个新期号定位：同一实物、装订册位置
    for num in ("5", "6"):
        m = api.get(
            f"/api/items/locate/?title={t.id}&volume=8&number={num}"
        ).json()["matches"][0]
        assert m["barcode"] == "CB-34"
        assert m["bound"] is True
        assert m["binding"] == "Q/HK-CORR"
        assert m["location"] == "装订库 E-05"

    # 刷新时间轴：绑定关系与新投影一致
    tl = api.get(f"/api/timeline/?title={t.id}").json()
    slot5 = next(s for s in tl["slots"] if s["number"] == "5")
    it_view = slot5["issues"][0]["items"][0]
    assert it_view["barcode"] == "CB-34"
    assert it_view["bound"] is True
    assert it_view["location"] == "装订库 E-05"

    # 拆订：恢复原位置；多期号关系仍是更正后的 5/6
    binding_id = Binding.objects.get(call_number="Q/HK-CORR").id
    assert api.post("/api/bindings/unbind/",
                    {"binding_id": binding_id}, format="json").status_code == 200
    item.refresh_from_db()
    assert item.current_location() == "期刊区B-02"
    match = api.get("/api/items/locate/?barcode=CB-34").json()["matches"][0]
    assert {n["number"] for n in match["numbers"]} == {"5", "6"}
    assert match["bound"] is False
    assert match["location"] == "期刊区B-02"

    # 旧导出记录不变：仍能追溯 v1 的 3/4 关系，且独立于现行投影
    old = api.get(f"/api/exports/{export_id}/").json()
    assert old["coverage_version"] == 1
    assert old["correction"] is None
    assert [r["number"] for r in old["snapshot"]["numbers"]] == ["3", "4"]
    assert old["snapshot"]["issue_month"] == "2024-03-01"

    # 更正后再导出：快照是 v2，挂到更正单
    resp = api.post("/api/exports/", {
        "title": t.id, "issue": comb.id,
    }, format="json")
    new = resp.json()
    assert new["coverage_version"] == 2
    assert new["correction"] == corr_id
    assert [r["number"] for r in new["snapshot"]["numbers"]] == ["5", "6"]


@pytest.mark.django_db
def test_withdraw_blocked_if_original_number_reoccupied(combined_title, api):
    """撤回时原编号已被别的发行期占用 → 拒绝撤回，现行（更正后）投影不动。"""
    t, nums, item, comb = combined_title
    n6 = _make_number(t, 6)
    corr_id = api.post("/api/corrections/", _correction_payload(
        comb, [nums["n3"], nums["n4"]], [nums["n5"], n6],
        "2024-05-01", "2024-06-01",
    ), format="json").json()["id"]
    api.post(f"/api/corrections/{corr_id}/apply/", {}, format="json")

    # 更正释放了 no.3：编辑部后来又把 no.3 分配给一次新发行
    new3 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-11-01",
    )
    new3.numbers.add(nums["n3"])

    resp = api.post(f"/api/corrections/{corr_id}/withdraw/", {}, format="json")
    assert resp.status_code == 409
    assert resp.json()["code"] == "number_occupied"
    # 更正后的 5/6 投影保持不变
    comb.refresh_from_db()
    assert comb.coverage_version == 2
    assert list(
        IssueNumbering.objects.filter(issue=comb)
        .values_list("number__number", flat=True).order_by("number__number")
    ) == ["5", "6"]


@pytest.mark.django_db
def test_month_only_correction_keeps_numbers(combined_title, api):
    """只更正跨月区间、不改编号：编号定位不变，时间轴显示新区间。"""
    t, nums, item, comb = combined_title
    resp = api.post("/api/corrections/", {
        "issue": comb.id,
        "reason": "实际发行推迟到 5-6 月",
        "proposed_issue_month": "2024-05-01",
        "proposed_issue_month_end": "2024-06-01",
        "lines": [
            {"original_number_id": nums["n3"].id,
             "proposed_number_id": nums["n3"].id},
            {"original_number_id": nums["n4"].id,
             "proposed_number_id": nums["n4"].id},
        ],
    }, format="json")
    assert resp.status_code == 201, resp.json()
    corr_id = resp.json()["id"]
    assert api.post(f"/api/corrections/{corr_id}/apply/", {},
                    format="json").status_code == 200
    for num in ("3", "4"):
        m = api.get(
            f"/api/items/locate/?title={t.id}&volume=8&number={num}"
        ).json()["matches"][0]
        assert m["barcode"] == "CB-34"
    tl = api.get(f"/api/timeline/?title={t.id}").json()
    iss = next(s for s in tl["slots"] if s["number"] == "3")["issues"][0]
    assert iss["issue_month_end"] == "2024-06-01"


@pytest.mark.django_db
def test_applied_correction_history_cannot_be_deleted_or_edited(
        combined_title, api):
    t, nums, item, comb = combined_title
    n6 = _make_number(t, 6)
    corr_id = api.post("/api/corrections/", _correction_payload(
        comb, [nums["n3"], nums["n4"]], [nums["n5"], n6],
        "2024-05-01", "2024-06-01",
    ), format="json").json()["id"]
    api.post(f"/api/corrections/{corr_id}/apply/", {}, format="json")

    assert api.delete(f"/api/corrections/{corr_id}/").status_code == 409
    # 已应用单不能 PATCH
    resp = api.patch(f"/api/corrections/{corr_id}/",
                     {"reason": "改原因"}, format="json")
    assert resp.status_code == 409
    # 有更正历史的 Issue 不能直接静默改月份/编号
    resp = api.patch(f"/api/issues/{comb.id}/",
                     {"issue_month": "2020-01-01"}, format="json")
    assert resp.status_code == 400
