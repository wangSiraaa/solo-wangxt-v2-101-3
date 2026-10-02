from django.db import transaction
from rest_framework import serializers

from .models import (
    Binding, BindingEntry, CorrectionEvent, CorrectionLine, ExportRecord,
    Issue, IssueCorrection, IssueNumber, IssueNumbering, Item, Title,
    assert_no_pending_correction, build_coverage_snapshot,
    latest_applied_correction, validate_proposed_coverage,
    CorrectionConflict,
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
            "coverage_version",
        ]

    def validate(self, attrs):
        title = attrs.get("title", getattr(self.instance, "title", None))
        numbers = attrs.get("numbers")
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if self.instance is not None:
            # 已有更正单（草稿/已应用/已撤回）后，覆盖关系只能走更正流程，
            # 不能静默改掉已装订、定位或导出所依据的历史投影。
            coverage_fields = {
                "numbers": numbers,
                "issue_month": attrs.get("issue_month"),
                "issue_month_end": attrs.get("issue_month_end"),
            }
            changes = {k: v for k, v in coverage_fields.items() if v is not None}
            if changes and IssueCorrection.objects.filter(issue=self.instance).exists():
                corr = IssueCorrection.objects.filter(
                    issue=self.instance,
                    status=IssueCorrection.Status.DRAFT,
                ).first()
                hint = (
                    f"（未决更正单 #{corr.id}）" if corr else
                    "（该发行已有更正历史；请新建发行更正单）"
                )
                raise serializers.ValidationError(
                    f"不能直接改写覆盖期号/发行区间{hint}。",
                )
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
        # 该发行存在草稿更正单时，入藏会让历史/新投影边界不清：先决更正
        if self.instance is None and issue is not None:
            assert_no_pending_correction(issue, action="入藏")
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
        blocked = []
        for it in deduped:
            try:
                assert_no_pending_correction(it.issue, action="装订")
            except CorrectionConflict as exc:
                blocked.append(f"{it.barcode}（{exc.detail}）")
        if blocked:
            raise CorrectionConflict(
                "以下实物的发行期存在未决更正单，请先处理更正："
                + "；".join(blocked),
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

class CorrectionLineInputSerializer(serializers.Serializer):
    """更正单行输入：original_number_id 必须是该 Issue 现行覆盖；
    proposed_number_id 可空（撤销该覆盖，普通期不允许，领域层再校验）。"""

    original_number_id = serializers.PrimaryKeyRelatedField(
        queryset=IssueNumber.objects.all(), allow_null=True,
    )
    proposed_number_id = serializers.PrimaryKeyRelatedField(
        queryset=IssueNumber.objects.all(), allow_null=True,
    )
    proposed_label = serializers.CharField(
        max_length=40, required=False, allow_blank=True, default="",
    )

    def validate(self, attrs):
        if attrs.get("original_number_id") is None and \
                attrs.get("proposed_number_id") is None:
            raise serializers.ValidationError(
                "更正行至少需要一个原编号或拟议编号。",
            )
        return attrs


class CorrectionLineOutputSerializer(serializers.ModelSerializer):
    original = serializers.SerializerMethodField()
    proposed = serializers.SerializerMethodField()

    class Meta:
        model = CorrectionLine
        fields = ["seq", "original", "proposed", "proposed_label"]

    def get_original(self, obj):
        n = obj.original_number
        return None if n is None else {
            "number_id": n.id, "volume": n.volume, "number": n.number,
        }

    def get_proposed(self, obj):
        n = obj.proposed_number
        return None if n is None else {
            "number_id": n.id, "volume": n.volume, "number": n.number,
        }


class CorrectionEventSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = CorrectionEvent
        fields = [
            "id", "action", "action_label", "detail", "expected_version",
            "issue_version_after", "actor", "created_at",
        ]


class IssueCorrectionSerializer(serializers.ModelSerializer):
    """发行更正单。lines 为成对的原编号→拟议编号，整张单一个事务。"""

    lines = CorrectionLineInputSerializer(many=True, write_only=True)
    lines_detail = CorrectionLineOutputSerializer(
        source="lines", many=True, read_only=True,
    )
    events = CorrectionEventSerializer(many=True, read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = IssueCorrection
        fields = [
            "id", "issue", "status", "status_label", "reason",
            "proposed_issue_month", "proposed_issue_month_end",
            "original_numbers", "original_issue_month",
            "original_issue_month_end",
            "applied_issue_version", "withdrawn_issue_version",
            "version", "client_token",
            "created_at", "updated_at", "applied_at", "withdrawn_at",
            "lines", "lines_detail", "events",
        ]
        read_only_fields = [
            "status", "original_numbers", "original_issue_month",
            "original_issue_month_end", "applied_issue_version",
            "withdrawn_issue_version", "version",
            "created_at", "updated_at", "applied_at", "withdrawn_at",
        ]

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("更正单至少包含一行期号更正。")
        pairs = [
            (ln["original_number_id"].id if ln["original_number_id"] else None,
             ln["proposed_number_id"].id if ln["proposed_number_id"] else None)
            for ln in lines
        ]
        originals = [p[0] for p in pairs if p[0] is not None]
        proposed = [p[1] for p in pairs if p[1] is not None]
        if len(originals) != len(set(originals)):
            raise serializers.ValidationError("同一原编号不能出现在多行。")
        if len(proposed) != len(set(proposed)):
            raise serializers.ValidationError("同一拟议编号不能出现在多行。")
        return lines

    def validate(self, attrs):
        issue = attrs.get("issue") or getattr(self.instance, "issue", None)
        if self.instance is not None and \
                self.instance.status != IssueCorrection.Status.DRAFT:
            raise CorrectionConflict(
                f"更正单 #{self.instance.id} 已"
                f"{self.instance.get_status_display()}，不能修改；"
                "如需调整请撤回后新建。",
                code="not_editable",
            )
        # PATCH 只改原因/区间时，lines 与区间沿用实例现值做校验
        if self.instance is not None and "lines" not in attrs:
            lines = [
                {
                    "original_number_id": ln.original_number,
                    "proposed_number_id": ln.proposed_number,
                    "proposed_label": ln.proposed_label,
                }
                for ln in self.instance.lines.all()
            ]
        else:
            lines = attrs["lines"]
        start = attrs.get(
            "proposed_issue_month",
            getattr(self.instance, "proposed_issue_month", None),
        )
        end = attrs.get(
            "proposed_issue_month_end",
            getattr(self.instance, "proposed_issue_month_end", None),
        )
        if end and start and end < start:
            raise serializers.ValidationError(
                {"proposed_issue_month_end": "拟议截止年月不能早于起始年月。"},
            )
        # 现行覆盖必须逐行对得上——更正单保存的是「原覆盖关系」，不能凭空改
        current = list(
            IssueNumbering.objects.filter(issue=issue)
            .select_related("number")
        )
        current_ids = {nn.number_id for nn in current}
        originals = {
            ln["original_number_id"].id
            for ln in lines if ln["original_number_id"] is not None
        }
        missing = current_ids - originals
        extra = originals - current_ids
        if missing or extra:
            raise serializers.ValidationError({
                "lines": (
                    f"原编号与该发行现行覆盖不一致；缺失 {sorted(missing)}，"
                    f"多余 {sorted(extra)}。"
                ),
            })
        # 落草稿前就做整张单的占用校验（事务级冲突在应用时还会再验一次）
        proposed_ids = [
            ln["proposed_number_id"].id
            for ln in lines if ln["proposed_number_id"] is not None
        ]
        try:
            validate_proposed_coverage(issue, proposed_ids, start, end)
        except CorrectionConflict as exc:
            raise serializers.ValidationError({"lines": exc.detail})
        if IssueCorrection.objects.filter(
            issue=issue, status=IssueCorrection.Status.DRAFT,
        ).exists():
            raise serializers.ValidationError(
                "该发行期已存在草稿更正单，请先处理（应用/修改/删除）。",
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        issue = validated_data["issue"]
        current = list(
            IssueNumbering.objects.filter(issue=issue)
            .select_related("number").order_by("id")
        )
        validated_data["original_numbers"] = [
            {
                "number_id": nn.number_id,
                "volume": nn.number.volume,
                "number": nn.number.number,
                "label": nn.label,
            }
            for nn in current
        ]
        validated_data["original_issue_month"] = issue.issue_month
        validated_data["original_issue_month_end"] = issue.issue_month_end
        corr = IssueCorrection.objects.create(**validated_data)
        CorrectionLine.objects.bulk_create([
            CorrectionLine(
                correction=corr,
                seq=i,
                original_number=ln["original_number_id"],
                proposed_number=ln["proposed_number_id"],
                proposed_label=ln.get("proposed_label", ""),
            )
            for i, ln in enumerate(lines)
        ])
        CorrectionEvent.objects.create(
            correction=corr, action=CorrectionEvent.Action.CREATE,
            detail=f"创建草稿；原覆盖版本 v{issue.coverage_version}",
            issue_version_after=issue.coverage_version,
        )
        return corr

    @transaction.atomic
    def update(self, instance, validated_data):
        if instance.status != IssueCorrection.Status.DRAFT:
            raise CorrectionConflict(
                f"更正单 #{instance.id} 已{instance.get_status_display()}，"
                "不能修改；如需调整请撤回后新建。",
                code="not_editable",
            )
        lines = validated_data.pop("lines", None)
        issue = instance.issue
        for field in ("reason", "proposed_issue_month",
                      "proposed_issue_month_end", "client_token"):
            if field in validated_data:
                setattr(instance, field, validated_data[field])
        instance.version += 1
        instance.save()
        if lines is not None:
            proposed_ids = [
                ln["proposed_number_id"].id
                for ln in lines if ln["proposed_number_id"] is not None
            ]
            validate_proposed_coverage(
                issue, proposed_ids,
                instance.proposed_issue_month,
                instance.proposed_issue_month_end,
            )
            instance.lines.all().delete()
            CorrectionLine.objects.bulk_create([
                CorrectionLine(
                    correction=instance, seq=i,
                    original_number=ln["original_number_id"],
                    proposed_number=ln["proposed_number_id"],
                    proposed_label=ln.get("proposed_label", ""),
                )
                for i, ln in enumerate(lines)
            ])
        CorrectionEvent.objects.create(
            correction=instance, action=CorrectionEvent.Action.UPDATE,
            detail=f"草稿已修改；更正单版本 v{instance.version}",
        )
        return instance


class CorrectionActionSerializer(serializers.Serializer):
    """应用/撤回动作的入参：expected_version 用于乱序检测。"""

    expected_version = serializers.IntegerField(required=False, allow_null=True)
    actor = serializers.CharField(required=False, allow_blank=True, default="")


# ---------- 导出记录 ----------

class ExportRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExportRecord
        fields = [
            "id", "title", "issue", "coverage_version", "correction",
            "snapshot", "note", "created_at",
        ]
        read_only_fields = [
            "coverage_version", "snapshot", "created_at", "correction",
        ]

    @transaction.atomic
    def create(self, validated_data):
        issue = validated_data["issue"]
        title = validated_data["title"]
        if issue.title_id != title.id:
            raise serializers.ValidationError("导出的发行期不属于该刊。")
        snapshot = build_coverage_snapshot(issue)
        correction = latest_applied_correction(issue)
        return ExportRecord.objects.create(
            title=title, issue=issue,
            coverage_version=issue.coverage_version,
            correction=correction,
            snapshot=snapshot,
            note=validated_data.get("note", ""),
        )

