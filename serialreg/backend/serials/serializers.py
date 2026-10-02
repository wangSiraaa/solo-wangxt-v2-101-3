from django.db import transaction
from rest_framework import serializers

from . import corrections
from .models import (
    Binding, BindingEntry, CorrectionOrder, ExportSnapshot, Issue,
    IssueNumber, IssueNumbering, Item, Title,
)


class TitleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Title
        fields = [
            "id", "title", "issn", "publisher", "status",
            "ceased_month", "created_at",
        ]

    def validate(self, attrs):
        status = attrs.get("status", getattr(self.instance, "status", None))
        ceased_month = attrs.get(
            "ceased_month", getattr(self.instance, "ceased_month", None),
        )
        if status == Title.PublicationStatus.CEASED and not ceased_month:
            raise serializers.ValidationError(
                {"ceased_month": "停刊刊名必须填写停刊月份。"},
            )
        return attrs


class IssueNumberSerializer(serializers.ModelSerializer):
    class Meta:
        model = IssueNumber
        fields = ["id", "title", "volume", "number", "sort_key"]


class IssueNumberingSerializer(serializers.ModelSerializer):
    number_id = serializers.IntegerField(source="number.id", read_only=True)
    volume = serializers.CharField(source="number.volume", read_only=True)
    number = serializers.CharField(source="number.number", read_only=True)
    label = serializers.CharField()

    class Meta:
        model = IssueNumbering
        fields = ["number_id", "volume", "number", "label"]


class IssueSerializer(serializers.ModelSerializer):
    """发行期。numbers 为期号 id 列表：普通期 1 个，合刊 ≥2 个。"""

    number_ids = serializers.PrimaryKeyRelatedField(
        queryset=IssueNumber.objects.all(),
        many=True, write_only=True, source="numbers",
    )
    numberings = IssueNumberingSerializer(many=True, read_only=True)

    class Meta:
        model = Issue
        fields = [
            "id", "title", "kind", "issue_month", "issue_month_end",
            "note", "number_ids", "numberings", "created_at",
        ]

    def validate(self, attrs):
        title = attrs.get("title", getattr(self.instance, "title", None))
        numbers = attrs.get("numbers")
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if numbers is not None:
            if len(numbers) < 1:
                raise serializers.ValidationError(
                    {"number_ids": "至少关联一个期号。"},
                )
            wrong = [n.id for n in numbers if n.title_id != title.id]
            if wrong:
                raise serializers.ValidationError(
                    {"number_ids": f"期号 {wrong} 不属于该刊。"},
                )
            if len({n.id for n in numbers}) != len(numbers):
                raise serializers.ValidationError(
                    {"number_ids": "期号不能重复。"},
                )
            if kind == Issue.IssueKind.COMBINED and len(numbers) < 2:
                raise serializers.ValidationError(
                    {"number_ids": "合刊必须关联至少两个期号。"},
                )
            if kind == Issue.IssueKind.REGULAR and len(numbers) != 1:
                raise serializers.ValidationError(
                    {"number_ids": "普通期只能关联一个期号。"},
                )
            # 一个编号槽位只能被发行一次
            qs = IssueNumbering.objects.filter(number__in=numbers)
            if self.instance:
                qs = qs.exclude(issue=self.instance)
            if qs.exists():
                used = list(
                    qs.values_list("number__volume", "number__number", "issue_id"),
                )
                raise serializers.ValidationError(
                    {"number_ids": f"期号已被其他发行期占用：{used}"},
                )
        start = attrs.get("issue_month", getattr(self.instance, "issue_month", None))
        end = attrs.get(
            "issue_month_end", getattr(self.instance, "issue_month_end", None),
        )
        if start and end and end < start:
            raise serializers.ValidationError(
                {"issue_month_end": "截止年月不能早于起始年月。"},
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        numbers = validated_data.pop("numbers")
        issue = Issue.objects.create(**validated_data)
        labels = self._labels(issue, numbers)
        IssueNumbering.objects.bulk_create([
            IssueNumbering(issue=issue, number=n, label=labels.get(n.id, ""))
            for n in numbers
        ])
        return issue

    def _labels(self, issue, numbers):
        raw = self.initial_data.get("labels") or {}
        labels = {}
        if isinstance(raw, dict):
            for n in numbers:
                labels[n.id] = str(raw.get(str(n.id), raw.get(n.id, "")))
        if not any(labels.values()):
            joined = "-".join(n.number for n in numbers)
            labels = {n.id: f"no.{joined}" if len(numbers) > 1 else "" for n in numbers}
        return labels


class ItemSerializer(serializers.ModelSerializer):
    """入藏实物。current_location 在装订后取装订册位置。"""

    current_location = serializers.SerializerMethodField()
    bound = serializers.SerializerMethodField()
    binding_call_number = serializers.SerializerMethodField()
    number_ids = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            "id", "barcode", "title", "issue", "location", "status",
            "accessioned_at", "current_location", "bound",
            "binding_call_number", "number_ids",
        ]
        read_only_fields: list = []

    def validate_status(self, value):
        # status 可经 PATCH 修改（报失/找回）；bound 只能由装订/拆订流程设置
        if value == Item.ItemStatus.BOUND:
            raise serializers.ValidationError(
                "已装订状态只能通过装订/拆订操作变更。",
            )
        return value

    def get_current_location(self, obj):
        return obj.current_location()

    def get_bound(self, obj):
        return obj.is_bound

    def get_binding_call_number(self, obj):
        return obj.binding_entry.binding.call_number if obj.is_bound else None

    def get_number_ids(self, obj):
        return list(obj.issue.numbers.values_list("id", flat=True))

    def validate(self, attrs):
        title = attrs.get("title", getattr(self.instance, "title", None))
        issue = attrs.get("issue", getattr(self.instance, "issue", None))
        if issue and title and issue.title_id != title.id:
            raise serializers.ValidationError("实物所属刊与发行期不一致。")
        # 新建入藏时，若该发行期挂着未应用的草稿更正单，给出明确冲突提示
        # （报失/状态 PATCH 不经过这里的 issue 变更，不受影响）
        if self.instance is None and issue is not None:
            from .corrections import CorrectionConflict
            pending_qs = CorrectionOrder.objects.filter(
                status=CorrectionOrder.Status.DRAFT,
                lines__issue=issue,
            )
            if pending_qs.exists():
                refs = "、".join(
                    f"#{i}" for i in
                    pending_qs.values_list("id", flat=True).distinct()
                )
                raise CorrectionConflict(
                    f"发行期 #{issue.id} 存在未应用的草稿更正单（{refs}），"
                    "请先应用或撤回更正单再入藏，避免实物挂在即将变更的关系上。",
                    code="pending_correction",
                )
        return attrs


class BindingSerializer(serializers.ModelSerializer):
    item_ids = serializers.PrimaryKeyRelatedField(
        queryset=Item.objects.all(), many=True, write_only=True,
    )
    items = serializers.SerializerMethodField()

    class Meta:
        model = Binding
        fields = [
            "id", "call_number", "title", "location", "bound_month",
            "item_ids", "items", "created_at",
        ]

    def get_items(self, obj):
        return [
            {
                "barcode": e.item.barcode,
                "previous_location": e.previous_location,
                "current_location": obj.location,
            }
            for e in obj.entries.select_related("item")
        ]

    def validate_item_ids(self, items):
        if not items:
            raise serializers.ValidationError("装订册至少包含一个实物。")
        # 合刊实物会同时挂在多个期号下，前端可能重复勾选：按主键去重
        deduped = list({it.id: it for it in items}.values())
        already = [it.barcode for it in deduped if it.is_bound]
        if already:
            raise serializers.ValidationError(
                f"实物已在装订册中：{already}，请先拆订。",
            )
        # 后续装订冲突提示：实物所属发行期挂着未应用的草稿更正单时，
        # 不允许先装订——否则装订证据与拟议关系会产生歧义
        from .corrections import CorrectionConflict
        rows = CorrectionOrder.objects.filter(
            status=CorrectionOrder.Status.DRAFT,
            lines__issue_id__in={it.issue_id for it in deduped},
        ).values_list("lines__issue_id", "id").distinct()
        issue_to_orders = {}
        for issue_id, order_id in rows:
            issue_to_orders.setdefault(issue_id, set()).add(order_id)
        if issue_to_orders:
            issue_ids = sorted(issue_to_orders)
            order_ids = sorted({oid for ids in issue_to_orders.values() for oid in ids})
            refs = "、".join(f"#{i}" for i in order_ids)
            barcodes = [
                it.barcode for it in deduped if it.issue_id in issue_to_orders
            ]
            raise CorrectionConflict(
                f"实物 {barcodes} 所属发行期 {issue_ids} 存在未应用的草稿更正单"
                f"（{refs}），请先应用或撤回更正单再装订。",
                code="pending_correction",
            )
        return deduped

    def validate(self, attrs):
        title = attrs["title"]
        bad = [it.barcode for it in attrs["item_ids"] if it.title_id != title.id]
        if bad:
            raise serializers.ValidationError(
                {"item_ids": f"以下实物不属于该刊，不能混装：{bad}"},
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop("item_ids")
        binding = Binding.objects.create(**validated_data)
        BindingEntry.objects.bulk_create([
            BindingEntry(
                item=it, binding=binding, previous_location=it.location or "",
            )
            for it in items
        ])
        Item.objects.filter(id__in=[it.id for it in items]).update(
            status=Item.ItemStatus.BOUND,
        )
        return binding


class UnbindSerializer(serializers.Serializer):
    """拆订：恢复每个实物装订前的位置与在馆状态。"""

    binding_id = serializers.PrimaryKeyRelatedField(
        queryset=Binding.objects.all(),
    )

    @transaction.atomic
    def save(self, **kwargs):
        binding = self.validated_data["binding_id"]
        entries = list(binding.entries.select_related("item"))
        for e in entries:
            e.item.location = e.previous_location
            e.item.status = Item.ItemStatus.AVAILABLE
            e.item.save(update_fields=["location", "status"])
        BindingEntry.objects.filter(binding=binding).delete()
        binding.delete()
        return [e.item for e in entries]


# ---------- 发行更正单 ----------

class CorrectionLineWriteSerializer(serializers.Serializer):
    """更正单的一行：针对一个发行期，给拟议月份与拟议覆盖编号。

    number_ids 省略/为 null 表示该行只更正月份、不动编号；
    issue_month 省略表示只更正编号、不动月份。
    """

    issue = serializers.PrimaryKeyRelatedField(queryset=Issue.objects.all())
    kind = serializers.ChoiceField(
        choices=Issue.IssueKind.choices, required=False, allow_null=True,
    )
    issue_month = serializers.DateField(required=False, allow_null=True)
    issue_month_end = serializers.DateField(required=False, allow_null=True)
    number_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True,
    )
    labels = serializers.DictField(required=False)


class CorrectionWriteSerializer(serializers.Serializer):
    title = serializers.PrimaryKeyRelatedField(queryset=Title.objects.all())
    reason = serializers.CharField(max_length=255)
    client_ref = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default="",
    )
    lines = CorrectionLineWriteSerializer(many=True)

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("更正单至少包含一条更正行。")
        issue_ids = [ln["issue"].id for ln in lines]
        if len(set(issue_ids)) != len(issue_ids):
            raise serializers.ValidationError("同一发行期在单内不能出现两次。")
        return lines

    def validate(self, attrs):
        title = attrs["title"]
        for ln in attrs["lines"]:
            issue = ln["issue"]
            if issue.title_id != title.id:
                raise serializers.ValidationError(
                    {"lines": f"发行期 #{issue.id} 不属于该刊。"},
                )
            if ln.get("issue_month") and ln.get("issue_month_end") and (
                ln["issue_month_end"] < ln["issue_month"]
            ):
                raise serializers.ValidationError(
                    {"lines": f"发行期 #{issue.id} 截止年月不能早于起始年月。"},
                )
        return attrs

    def to_proposals(self):
        out = []
        raw_lines = []
        for ln in self.validated_data["lines"]:
            raw = {"issue": ln["issue"]}
            if ln.get("kind"):
                raw["kind"] = ln["kind"]
            if ln.get("issue_month"):
                raw["issue_month"] = ln["issue_month"]
            if "issue_month_end" in ln:
                raw["issue_month_end"] = ln["issue_month_end"]
            if ln.get("number_ids") is not None:
                raw["number_ids"] = ln["number_ids"]
            if ln.get("labels") is not None:
                raw["labels"] = ln["labels"]
            out.append(raw)
            raw_lines.append(ln)
        return out, raw_lines


class NumberSnapshotSerializer(serializers.Serializer):
    number_id = serializers.IntegerField(allow_null=True)
    volume = serializers.CharField()
    number = serializers.CharField()
    label = serializers.CharField()


class CorrectionSideSerializer(serializers.Serializer):
    kind = serializers.CharField()
    issue_month = serializers.DateField()
    issue_month_end = serializers.DateField(allow_null=True)
    numbers = NumberSnapshotSerializer(many=True)


class CorrectionLineReadSerializer(serializers.Serializer):
    issue_id = serializers.IntegerField()
    status = serializers.CharField()
    version_before = serializers.IntegerField(allow_null=True)
    version_after = serializers.IntegerField(allow_null=True)
    before = CorrectionSideSerializer()
    after = CorrectionSideSerializer()


class CorrectionEventSerializer(serializers.Serializer):
    action = serializers.CharField()
    detail = serializers.CharField()
    created_at = serializers.DateTimeField()


def order_payload(order):
    """更正单读模型：头信息 + 每行原/拟议双侧 + 审计事件。"""
    from .corrections import line_payload
    lines = (
        order.lines.prefetch_related("number_snapshots")
        .select_related("order")
    )
    return {
        "id": order.id,
        "title": order.title_id,
        "reason": order.reason,
        "status": order.status,
        "client_ref": order.client_ref,
        "created_at": order.created_at,
        "applied_at": order.applied_at,
        "withdrawn_at": order.withdrawn_at,
        "lines": [
            {
                "issue_id": line.issue_id,
                "status": line.order.status,
                "version_before": line.version_before,
                "version_after": line.version_after,
                "before": line_payload(line)["before"],
                "after": line_payload(line)["after"],
            }
            for line in lines
        ],
        "events": [
            CorrectionEventSerializer(e).data
            for e in order.events.all()
        ],
    }


class ExportSnapshotSerializer(serializers.ModelSerializer):
    """导出/定位证据快照。旧导出永远展示它冻结时的版本。"""

    class Meta:
        model = ExportSnapshot
        fields = [
            "id", "title", "issue", "item", "issue_version", "barcode",
            "month_start", "month_end", "location", "numbers_json",
            "created_at",
        ]
        read_only_fields = [
            "issue", "item", "issue_version", "barcode", "month_start",
            "month_end", "location", "numbers_json",
        ]
