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
from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError


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
    # 投影版本：每应用/撤回一次涉及本期的更正单 +1；导出快照、撤回冲突判定都靠它
    version = models.PositiveIntegerField("投影版本", default=1)
    last_corrected_at = models.DateTimeField("最近更正时间", null=True, blank=True)

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
    """从任一期号找到其所在实物与实际位置（合刊、装订都可命中）。

    更正单应用后，命中关系直接来自更正后的 IssueNumbering 投影；
    历史关系在 CorrectionNumberSnapshot 里留痕，不参与实时定位。
    """
    rows = []
    for issue in number.issues.all():
        for item in issue.items.select_related("title"):
            rows.append({
                "issue_id": issue.id,
                "issue_version": issue.version,
                "barcode": item.barcode,
                "status": item.status,
                "location": item.current_location(),
                "bound": item.is_bound,
                "binding": item.binding_entry.binding.call_number if item.is_bound else None,
            })
    return rows


class CorrectionOrder(models.Model):
    """发行更正单：入藏后由编辑部发起的覆盖期号/发行区间更正。

    更正单不原地改写历史：草稿封存原关系与拟议关系；应用时在一个事务里
    重写 IssueNumbering/月份投影并把 Issue.version+1；撤回（已应用单）则
    按原关系反向重放。涉及合刊的多条关联随整张单子一起校验、一起提交，
    任何一条冲突都整张拒绝，不会留下半条关联。
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        APPLIED = "applied", "已应用"
        WITHDRAWN = "withdrawn", "已撤回"

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="corrections",
    )
    reason = models.CharField("更正原因", max_length=255)
    status = models.CharField(
        "状态", max_length=10,
        choices=Status.choices, default=Status.DRAFT, db_index=True,
    )
    # 客户端幂等键：同刊内重放同键的提交直接返回旧单，不重复建单
    client_ref = models.CharField("客户端幂等键", max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField("应用时间", null=True, blank=True)
    withdrawn_at = models.DateTimeField("撤回时间", null=True, blank=True)

    class Meta:
        verbose_name = "发行更正单"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["title", "client_ref"],
                condition=~Q(client_ref=""),
                name="uniq_correction_client_ref_per_title",
            ),
        ]

    def __str__(self):
        return f"更正单#{self.id}（{self.get_status_display()}）"


class CorrectionLine(models.Model):
    """更正单内一条发行期的「原覆盖 → 拟议覆盖」。

    月份在开单时从实时投影快照封存；拟议月份为空表示该行不改月份。
    合刊期的多个期号仍是多条独立编号快照，事务内整体校验。
    """

    order = models.ForeignKey(
        CorrectionOrder, on_delete=models.CASCADE, related_name="lines",
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT, related_name="correction_lines",
        help_text="更正只投影编号与发行区间，发行实体本身不变（实物/装订不断链）",
    )
    kind_before = models.CharField("原类型", max_length=10, choices=Issue.IssueKind.choices)
    kind_after = models.CharField("拟议类型", max_length=10, choices=Issue.IssueKind.choices)
    month_start_before = models.DateField("原发行起始月")
    month_end_before = models.DateField("原发行截止月", null=True, blank=True)
    month_start_after = models.DateField("拟议发行起始月")
    month_end_after = models.DateField("拟议发行截止月", null=True, blank=True)
    # 应用/撤回时该 Issue 的版本（version_before 撤回重放时用于乱序冲突判定）
    version_before = models.PositiveIntegerField("应用前版本", null=True, blank=True)
    version_after = models.PositiveIntegerField("应用后版本", null=True, blank=True)

    class Meta:
        verbose_name = "更正行"
        constraints = [
            models.UniqueConstraint(fields=["order", "issue"], name="uniq_line_issue_per_order"),
        ]


class CorrectionNumberSnapshot(models.Model):
    """编号关联快照（去规范化卷期，编号槽位删除后历史仍可还原）。"""

    class Side(models.TextChoices):
        BEFORE = "before", "原覆盖"
        AFTER = "after", "拟议覆盖"

    line = models.ForeignKey(
        CorrectionLine, on_delete=models.CASCADE, related_name="number_snapshots",
    )
    side = models.CharField("快照侧", max_length=10, choices=Side.choices)
    number = models.ForeignKey(
        IssueNumber, on_delete=models.PROTECT,
        null=True, blank=True, related_name="correction_snapshots",
    )
    # 去规范化：即便编号槽位将来被清理，旧定位证据仍读得到卷/期与封面标识
    volume = models.CharField("卷（快照）", max_length=20, blank=True)
    number_label = models.CharField("期（快照）", max_length=20)
    cover_label = models.CharField("封面标识（快照）", max_length=40, blank=True)

    class Meta:
        verbose_name = "编号关联快照"
        ordering = ["id"]
        indexes = [models.Index(fields=["side"])]


class CorrectionEvent(models.Model):
    """更正单审计事件：每一次状态变迁（含幂等重放）都追加，不覆盖。"""

    class Action(models.TextChoices):
        CREATE = "create", "开单"
        UPDATE = "update", "改单"
        APPLY = "apply", "应用"
        APPLY_REPLAY = "apply_replay", "重复应用（幂等）"
        WITHDRAW = "withdraw", "撤回"
        WITHDRAW_REPLAY = "withdraw_replay", "重复撤回（幂等）"
        REJECT = "reject", "拒绝"

    order = models.ForeignKey(
        CorrectionOrder, on_delete=models.CASCADE, related_name="events",
    )
    action = models.CharField("动作", max_length=20, choices=Action.choices)
    detail = models.CharField("说明", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "更正审计事件"
        ordering = ["created_at", "id"]


class ExportSnapshot(models.Model):
    """导出/定位证据快照：冻结导出那一刻的投影与版本号。

    更正单应用后实时投影变化，但本记录不动；旧导出凭 issue_version
    与冻结的期号/区间/位置信息永久追溯到更正前的版本。
    """

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="exports",
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT, related_name="exports",
    )
    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, null=True, blank=True,
        related_name="exports",
    )
    issue_version = models.PositiveIntegerField("导出时发行期版本")
    barcode = models.CharField("条码（快照）", max_length=40, blank=True)
    month_start = models.DateField("发行起始月（快照）")
    month_end = models.DateField("发行截止月（快照）", null=True, blank=True)
    location = models.CharField("实际位置（快照）", max_length=100, blank=True)
    numbers_json = models.JSONField("期号关系（快照）", default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "导出快照"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"导出#{self.id} issue={self.issue_id}@v{self.issue_version}"
