from django.contrib import admin

from .models import (
    Binding, BindingEntry, CorrectionEvent, CorrectionLine,
    CorrectionNumberSnapshot, CorrectionOrder, ExportSnapshot, Issue,
    IssueNumber, IssueNumbering, Item, Title,
)

admin.site.register(Title)
admin.site.register(IssueNumber)
admin.site.register(Issue)
admin.site.register(IssueNumbering)
admin.site.register(Item)
admin.site.register(Binding)
admin.site.register(BindingEntry)


class CorrectionLineInline(admin.TabularInline):
    model = CorrectionLine
    extra = 0
    show_change_link = True


@admin.register(CorrectionOrder)
class CorrectionOrderAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "reason", "created_at",
                    "applied_at", "withdrawn_at")
    list_filter = ("status",)
    inlines = [CorrectionLineInline]


@admin.register(CorrectionNumberSnapshot)
class CorrectionNumberSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "line", "side", "volume", "number_label",
                    "cover_label")
    list_filter = ("side",)


admin.site.register(CorrectionEvent)
admin.site.register(CorrectionLine)
admin.site.register(ExportSnapshot)
