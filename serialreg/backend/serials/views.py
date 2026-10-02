from django.db.models import Exists, OuterRef, Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    Binding, CorrectionEvent, ExportRecord, Issue, IssueCorrection,
    IssueNumber, IssueNumbering, Item, Title,
    apply_correction, build_coverage_snapshot, latest_applied_correction,
    locate_number, number_holding_status, withdraw_correction,
    CorrectionConflict,
)
from .serializers import (
    BindingSerializer, CorrectionActionSerializer, ExportRecordSerializer,
    IssueCorrectionSerializer, IssueSerializer, ItemSerializer,
    IssueNumberSerializer, TitleSerializer, UnbindSerializer,
)


class TitleViewSet(viewsets.ModelViewSet):
    queryset = Title.objects.all()
    serializer_class = TitleSerializer


class IssueNumberViewSet(viewsets.ModelViewSet):
    queryset = IssueNumber.objects.all()
    serializer_class = IssueNumberSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class IssueViewSet(viewsets.ModelViewSet):
    queryset = Issue.objects.prefetch_related(
        "numberings__number", "items",
    ).select_related("title")
    serializer_class = IssueSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class ItemViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.select_related(
        "title", "issue", "binding_entry__binding",
    ).prefetch_related("issue__numbers")
    serializer_class = ItemSerializer

    @action(detail=False, methods=["get"])
    def locate(self, request):
        """按 (title, volume, number) 或 barcode 定位实物。

        合刊的任一期号都必须能找到同一实物；装订后返回装订册位置。
        """
        title_id = request.query_params.get("title")
        volume = request.query_params.get("volume", "")
        number = request.query_params.get("number")
        barcode = request.query_params.get("barcode")

        if barcode:
            items = self.get_queryset().filter(barcode=barcode)
            issue_ids = [it.issue_id for it in items]
            pending = [
                {"issue_id": c.issue_id, "correction_id": c.id, "reason": c.reason}
                for c in IssueCorrection.objects.filter(
                    status=IssueCorrection.Status.DRAFT, issue_id__in=issue_ids,
                )
            ]
            result = []
            for it in items:
                result.append({
                    "barcode": it.barcode,
                    "issue_id": it.issue_id,
                    "coverage_version": it.issue.coverage_version,
                    "numbers": [
                        {"volume": n.volume, "number": n.number}
                        for n in it.issue.numbers.all()
                    ],
                    "location": it.current_location(),
                    "bound": it.is_bound,
                    "binding": it.binding_entry.binding.call_number
                    if it.is_bound else None,
                    "status": it.status,
                })
            return Response({
                "query": {"barcode": barcode},
                "pending_corrections": pending,
                "matches": result,
            })

        if not (title_id and number):
            return Response(
                {"detail": "需要提供 barcode，或同时提供 title 与 number。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = IssueNumber.objects.filter(
            title_id=title_id, number=number,
        )
        if volume != "":
            qs = qs.filter(volume=volume)
        try:
            issue_number = qs.get()
        except IssueNumber.DoesNotExist:
            # 编号本身未登记：区别于「已登记但无发行」的缺号
            return Response({
                "detail": "该卷期编号未在馆藏系统登记。",
                "holding_status": "unregistered",
                "matches": [],
            }, status=status.HTTP_404_NOT_FOUND)
        except IssueNumber.MultipleObjectsReturned:
            return Response(
                {"detail": "卷/期定位到多条编号，请补全卷号。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        matches = locate_number(issue_number)
        # 缺号（无发行记录）是正常业务状态，返回 200，不自动等同缺藏
        pending = [
            {"issue_id": c.issue_id, "correction_id": c.id, "reason": c.reason}
            for c in IssueCorrection.objects.filter(
                status=IssueCorrection.Status.DRAFT,
                issue__numberings__number=issue_number,
            )
        ]
        return Response({
            "query": {"title": title_id, "volume": volume, "number": number},
            "holding_status": number_holding_status(issue_number.title, issue_number),
            "pending_corrections": pending,
            "matches": matches,
        })


class BindingViewSet(viewsets.ModelViewSet):
    queryset = Binding.objects.prefetch_related(
        "entries__item",
    ).select_related("title")
    serializer_class = BindingSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs

    @action(detail=False, methods=["post"])
    def unbind(self, request):
        serializer = UnbindSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.save()
        return Response({
            "detail": "拆订完成，各实物已恢复原位置。",
            "restored": [
                {"barcode": it.barcode, "location": it.location,
                 "status": it.status}
                for it in items
            ],
        })


class TimelineViewSet(viewsets.ViewSet):
    """前端时间轴数据源：编号 × 发行 × 实物三层，外加停刊标记与更正投影。

    当前值（issue_month/numbers）直接来自应用更正后的现行投影；
    每个发行另带 corrections：原覆盖/拟议覆盖/状态/版本/审计，供前端
    同时展示当前值与历史值。
    """

    def list(self, request):
        title_id = request.query_params.get("title")
        if not title_id:
            return Response(
                {"detail": "需要提供 title 参数。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        title = Title.objects.get(pk=title_id)
        numbers = (
            IssueNumber.objects.filter(title=title)
            .prefetch_related(
                Prefetch(
                    "issues",
                    queryset=Issue.objects.prefetch_related(
                        Prefetch(
                            "numberings",
                            queryset=IssueNumbering.objects.select_related("number"),
                        ),
                        Prefetch(
                            "items",
                            queryset=Item.objects.select_related(
                                "binding_entry__binding",
                            ),
                        ),
                        Prefetch(
                            "corrections",
                            queryset=IssueCorrection.objects.prefetch_related(
                                "lines__original_number",
                                "lines__proposed_number",
                                "events",
                            ),
                        ),
                    ),
                ),
            )
            .order_by("sort_key", "id")
        )
        # 预先算出每个发行当前生效的更正单
        applied_map = {}
        for c in (
            IssueCorrection.objects.filter(
                issue__title=title, status=IssueCorrection.Status.APPLIED,
            ).select_related("issue")
        ):
            old = applied_map.get(c.issue_id)
            if old is None or (c.applied_at, c.id) > (old.applied_at, old.id):
                applied_map[c.issue_id] = c

        slots = []
        for n in numbers:
            issues = list(n.issues.all())
            items = [it for iss in issues for it in iss.items.all()]
            slots.append({
                "number_id": n.id,
                "volume": n.volume,
                "number": n.number,
                "holding_status": number_holding_status(title, n),
                "issues": [
                    self._issue_bundle(iss, applied_map.get(iss.id))
                    for iss in issues
                ],
            })
        return Response({
            "title": TitleSerializer(title).data,
            "slots": slots,
            "export_records": ExportRecordSerializer(
                ExportRecord.objects.filter(title=title)
                .select_related("correction")[:50], many=True,
            ).data,
        })

    @staticmethod
    def _issue_bundle(iss, active_correction):
        return {
            "issue_id": iss.id,
            "kind": iss.kind,
            "issue_month": iss.issue_month,
            "issue_month_end": iss.issue_month_end,
            "coverage_version": iss.coverage_version,
            "active_correction_id": active_correction.id
            if active_correction else None,
            "pending_correction_id": next(
                (c.id for c in iss.corrections.all()
                 if c.status == IssueCorrection.Status.DRAFT),
                None,
            ),
            "label": "·".join(
                f"{nn.number.volume}({nn.number.number})"
                for nn in iss.numberings.all()
            ),
            "combined_numbers": [
                {"number_id": nn.number_id,
                 "volume": nn.number.volume, "number": nn.number.number}
                for nn in iss.numberings.all()
            ],
            "items": [
                {
                    "item_id": it.id,
                    "barcode": it.barcode,
                    "status": it.status,
                    "location": it.current_location(),
                    "bound": it.is_bound,
                    "binding": it.binding_entry.binding.call_number
                    if it.is_bound else None,
                }
                for it in iss.items.all()
            ],
            "corrections": [
                {
                    "id": c.id,
                    "status": c.status,
                    "status_label": c.get_status_display(),
                    "reason": c.reason,
                    "version": c.version,
                    "proposed_issue_month": c.proposed_issue_month,
                    "proposed_issue_month_end": c.proposed_issue_month_end,
                    "original_issue_month": c.original_issue_month,
                    "original_issue_month_end": c.original_issue_month_end,
                    "applied_issue_version": c.applied_issue_version,
                    "withdrawn_issue_version": c.withdrawn_issue_version,
                    "created_at": c.created_at,
                    "applied_at": c.applied_at,
                    "withdrawn_at": c.withdrawn_at,
                    "original_numbers": c.original_numbers,
                    "lines": [
                        {
                            "original": None if ln.original_number is None else {
                                "number_id": ln.original_number_id,
                                "volume": ln.original_number.volume,
                                "number": ln.original_number.number,
                            },
                            "proposed": None if ln.proposed_number is None else {
                                "number_id": ln.proposed_number_id,
                                "volume": ln.proposed_number.volume,
                                "number": ln.proposed_number.number,
                            },
                            "proposed_label": ln.proposed_label,
                        }
                        for ln in c.lines.all()
                    ],
                    "events": [
                        {
                            "action": e.action,
                            "action_label": e.get_action_display(),
                            "detail": e.detail,
                            "created_at": e.created_at,
                        }
                        for e in c.events.all()
                    ],
                }
                for c in iss.corrections.all()
            ],
        }


class IssueCorrectionViewSet(viewsets.ModelViewSet):
    """发行更正单：草稿 → 应用 / 撤回。

    - 创建可带 client_token：同一令牌重复提交幂等返回，不重复落单；
      令牌被其他更正单占用时返回 409。
    - apply/withdraw 带 expected_version：乱序（旧版本重放）409；
      对同一状态重复动作幂等，不重复改变关系，只追加审计事件。
    """

    queryset = IssueCorrection.objects.prefetch_related(
        "lines__original_number", "lines__proposed_number", "events",
    ).select_related(
        "issue__title",
    )
    serializer_class = IssueCorrectionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        issue_id = self.request.query_params.get("issue")
        if issue_id:
            qs = qs.filter(issue_id=issue_id)
        elif title_id:
            qs = qs.filter(issue__title_id=title_id)
        return qs

    def create(self, request, *args, **kwargs):
        token = request.data.get("client_token") or None
        if token:
            existing = IssueCorrection.objects.filter(
                client_token=token,
            ).prefetch_related(
                "lines__original_number", "lines__proposed_number", "events",
            ).first()
            if existing is not None:
                # 同一创建意图的重复提交：幂等返回已存在的更正单
                return Response(
                    IssueCorrectionSerializer(existing).data,
                    status=status.HTTP_200_OK,
                    headers={"Idempotent-Replay": "true"},
                )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            instance = serializer.save()
        except CorrectionConflict as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code,
                 "conflicts": exc.conflicts},
                status=status.HTTP_409_CONFLICT,
            )
        headers = self.get_success_headers(serializer.data)
        return Response(
            IssueCorrectionSerializer(instance).data,
            status=status.HTTP_201_CREATED, headers=headers,
        )

    def update(self, request, *args, **kwargs):
        try:
            return super().update(request, *args, **kwargs)
        except CorrectionConflict as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code,
                 "conflicts": exc.conflicts},
                status=status.HTTP_409_CONFLICT,
            )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status != IssueCorrection.Status.DRAFT:
            return Response(
                {"detail": (
                    f"更正单 #{instance.id} 已{instance.get_status_display()}，"
                    "更正历史不能删除。"
                ), "code": "history_locked"},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)

    def _do_action(self, request, kind):
        correction = self.get_object()
        serializer = CorrectionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expected = serializer.validated_data.get("expected_version")
        actor = serializer.validated_data.get("actor", "")
        try:
            if kind == "apply":
                corr, changed = apply_correction(
                    correction, expected_version=expected, actor=actor,
                )
            else:
                corr, changed = withdraw_correction(
                    correction, expected_version=expected, actor=actor,
                )
        except CorrectionConflict as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code,
                 "conflicts": exc.conflicts},
                status=status.HTTP_409_CONFLICT,
            )
        corr = self.filter_queryset(self.get_queryset()).get(pk=corr.pk)
        resp = Response(
            IssueCorrectionSerializer(corr).data,
            headers={"State-Changed": "true" if changed else "false"},
        )
        return resp

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        return self._do_action(request, "apply")

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        return self._do_action(request, "withdraw")


class ExportRecordViewSet(viewsets.ModelViewSet):
    """导出/书目上报：创建瞬间冻结覆盖快照与版本号，之后不可变。"""

    queryset = ExportRecord.objects.select_related(
        "title", "issue", "correction",
    )
    serializer_class = ExportRecordSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        issue_id = self.request.query_params.get("issue")
        if issue_id:
            qs = qs.filter(issue_id=issue_id)
        elif title_id:
            qs = qs.filter(title_id=title_id)
        return qs
