from django.db.models import Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import corrections
from .models import (
    Binding, CorrectionOrder, ExportSnapshot, Issue, IssueNumber,
    IssueNumbering, Item, Title, locate_number, number_holding_status,
)
from .serializers import (
    BindingSerializer, CorrectionWriteSerializer, ExportSnapshotSerializer,
    IssueSerializer, ItemSerializer, IssueNumberSerializer, TitleSerializer,
    UnbindSerializer, order_payload,
)


def _raise_correction_error(exc):
    """把更正服务异常翻译成 HTTP（409 冲突 / 400 校验 / 400 状态迁移）。"""
    from .corrections import (
        CorrectionConflict, CorrectionStateError, CorrectionValidationError,
    )
    if isinstance(exc, CorrectionConflict):
        return Response(
            {"detail": exc.detail, "code": exc.code},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, CorrectionStateError):
        return Response(
            {"detail": exc.detail, "code": "invalid_state"},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, CorrectionValidationError):
        return Response(
            {"detail": exc.detail, "code": "invalid_correction"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    raise exc


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
            result = []
            for it in items:
                result.append({
                    "barcode": it.barcode,
                    "issue_id": it.issue_id,
                    "issue_version": it.issue.version,
                    "numbers": [
                        {"volume": n.volume, "number": n.number}
                        for n in it.issue.numbers.all()
                    ],
                    "issue_month": it.issue.issue_month,
                    "issue_month_end": it.issue.issue_month_end,
                    "location": it.current_location(),
                    "bound": it.is_bound,
                    "binding": it.binding_entry.binding.call_number
                    if it.is_bound else None,
                    "status": it.status,
                })
            return Response({"query": {"barcode": barcode}, "matches": result})

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
        return Response({
            "query": {"title": title_id, "volume": volume, "number": number},
            "holding_status": number_holding_status(issue_number.title, issue_number),
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
    """前端时间轴数据源：编号 × 发行 × 实物三层，外加停刊标记。"""

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
                    ),
                ),
            )
            .order_by("sort_key", "id")
        )
        slots = []
        # 批量取本刊所有发行期的更正（当前值 + 历史值）
        all_issue_ids = [
            iss.id for n in numbers for iss in n.issues.all()
        ]
        cindex = corrections.correction_index_for_issues(all_issue_ids)
        pending = corrections.pending_draft_issue_ids(title.id, all_issue_ids)
        for n in numbers:
            issues = list(n.issues.all())
            # 空槽位若在某张已应用更正单的 before 侧出现、after 侧消失，
            # 说明它是被更正释放的编号：当前是缺号，但历史覆盖仍可追溯
            formerly = []
            if not issues:
                for issue_id, info in cindex.items():
                    cur = info.get("current")
                    if not cur:
                        continue
                    before_has = any(x.get("number_id") == n.id
                                     for x in cur["before"]["numbers"])
                    after_has = any(x.get("number_id") == n.id
                                    for x in cur["after"]["numbers"])
                    if before_has and not after_has:
                        formerly.append({
                            "order_id": cur["order_id"],
                            "issue_id": issue_id,
                            "reason": cur["reason"],
                            "issued_before": {
                                "issue_month": cur["before"]["issue_month"],
                                "issue_month_end": cur["before"]["issue_month_end"],
                            },
                            "reassigned_to": [x["number"] for x in cur["after"]["numbers"]],
                        })
            slots.append({
                "number_id": n.id,
                "volume": n.volume,
                "number": n.number,
                "holding_status": number_holding_status(title, n),
                "formerly_issued": formerly,
                "issues": [
                    {
                        "issue_id": iss.id,
                        "version": iss.version,
                        "kind": iss.kind,
                        "issue_month": iss.issue_month,
                        "issue_month_end": iss.issue_month_end,
                        "has_pending_correction": iss.id in pending,
                        "current_correction": (cindex.get(iss.id) or {}).get(
                            "current"),
                        "correction_history": (cindex.get(iss.id) or {}).get(
                            "history", []),
                        "label": "·".join(
                            f"{nn.number.volume}({nn.number.number})"
                            for nn in iss.numberings.all()
                        ),
                        "combined_numbers": [
                            {"volume": nn.number.volume, "number": nn.number.number}
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
                    }
                    for iss in issues
                ],
            })
        return Response({
            "title": TitleSerializer(title).data,
            "slots": slots,
        })


class CorrectionOrderViewSet(viewsets.ModelViewSet):
    """发行更正单。

    POST   /corrections/           开草稿（同 title+client_ref 幂等）
    PATCH  /corrections/{id}/      改草稿
    POST   /corrections/{id}/apply/     应用（重复应用幂等）
    POST   /corrections/{id}/withdraw/  撤回（草稿/已应用均可，重复撤回幂等）
    """

    queryset = CorrectionOrder.objects.prefetch_related(
        "lines__number_snapshots", "events",
    ).select_related("title")
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        status_ = self.request.query_params.get("status")
        if title_id:
            qs = qs.filter(title_id=title_id)
        if status_:
            qs = qs.filter(status=status_)
        return qs

    def list(self, request, *args, **kwargs):
        data = [order_payload(o) for o in self.get_queryset()]
        return Response(data)

    def retrieve(self, request, *args, **kwargs):
        return Response(order_payload(self.get_object()))

    def create(self, request, *args, **kwargs):
        serializer = CorrectionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposals, _ = serializer.to_proposals()
        try:
            order, created = corrections.draft_correction(
                serializer.validated_data["title"],
                serializer.validated_data["reason"],
                proposals,
                client_ref=serializer.validated_data.get("client_ref", ""),
            )
        except corrections.CorrectionValidationError as exc:
            return _raise_correction_error(exc)
        resp_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        order = self.get_queryset().filter(pk=order.pk).first() or order
        return Response(order_payload(order), status=resp_status)

    def partial_update(self, request, *args, **kwargs):
        order = self.get_object()
        # 改单要求整单内容完整（更正单不做增量 patch，避免留下半行状态）
        serializer = CorrectionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proposals, _ = serializer.to_proposals()
        try:
            order = corrections.revise_draft(
                order,
                serializer.validated_data["reason"],
                proposals,
                client_ref=serializer.validated_data.get("client_ref", ""),
            )
        except (
            corrections.CorrectionValidationError,
            corrections.CorrectionStateError,
            corrections.CorrectionConflict,
        ) as exc:
            return _raise_correction_error(exc)
        return Response(order_payload(order))

    def destroy(self, request, *args, **kwargs):
        order = self.get_object()
        if order.status != CorrectionOrder.Status.DRAFT:
            return Response(
                {"detail": f"更正单#{order.id}已{order.get_status_display()}，"
                           "不能删除；只能撤回。",
                 "code": "invalid_state"},
                status=status.HTTP_409_CONFLICT,
            )
        order.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _transition(self, request, pk, func):
        order = self.get_object()
        try:
            order, changed = func(order)
        except (
            corrections.CorrectionValidationError,
            corrections.CorrectionStateError,
            corrections.CorrectionConflict,
        ) as exc:
            return _raise_correction_error(exc)
        payload = order_payload(self.get_queryset().get(pk=order.pk))
        payload["changed"] = changed
        return Response(payload)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        return self._transition(request, pk, corrections.apply_order)

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        return self._transition(request, pk, corrections.withdraw_order)


class ExportSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """导出快照：只读列表/详情；创建走 freeze 动作。"""

    queryset = ExportSnapshot.objects.select_related(
        "title", "issue", "item",
    )
    serializer_class = ExportSnapshotSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        issue_id = self.request.query_params.get("issue")
        if title_id:
            qs = qs.filter(title_id=title_id)
        if issue_id:
            qs = qs.filter(issue_id=issue_id)
        return qs

    @action(detail=False, methods=["post"])
    def freeze(self, request):
        """冻结一份导出证据：按 barcode 或 issue_id，记录当时的投影与版本。"""
        barcode = request.data.get("barcode")
        issue_id = request.data.get("issue")
        if barcode:
            item = Item.objects.select_related("issue", "title").filter(
                barcode=barcode,
            ).first()
            if item is None:
                return Response(
                    {"detail": f"条码 {barcode} 不存在。"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            issue, title, item_obj = item.issue, item.title, item
        elif issue_id:
            issue = Issue.objects.select_related("title").filter(
                pk=issue_id,
            ).first()
            if issue is None:
                return Response(
                    {"detail": f"发行期 #{issue_id} 不存在。"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            title, item_obj = issue.title, None
        else:
            return Response(
                {"detail": "需要提供 barcode 或 issue。"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        snapshot = ExportSnapshot.objects.create(
            title=title, issue=issue, item=item_obj,
            issue_version=issue.version,
            barcode=item_obj.barcode if item_obj else "",
            month_start=issue.issue_month,
            month_end=issue.issue_month_end,
            location=(item_obj.current_location() if item_obj else ""),
            numbers_json=[
                {"volume": nn.number.volume, "number": nn.number.number,
                 "label": nn.label}
                for nn in IssueNumbering.objects.filter(issue=issue)
                .select_related("number")
                .order_by("number__sort_key", "number__id")
            ],
        )
        return Response(
            ExportSnapshotSerializer(snapshot).data,
            status=status.HTTP_201_CREATED,
        )
