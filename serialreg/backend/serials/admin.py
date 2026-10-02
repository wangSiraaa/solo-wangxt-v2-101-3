from django.contrib import admin

from .models import (
    Binding, BindingEntry, CorrectionEvent, CorrectionLine, ExportRecord,
    Issue, IssueCorrection, IssueNumber, IssueNumbering, Item, Title,
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
    fk_name = "correction"


class CorrectionEventInline(admin.TabularInline):
    model = CorrectionEvent
    extra = 0


@admin.register(IssueCorrection)
class IssueCorrectionAdmin(admin.ModelAdmin):
    list_display = ("id", "issue", "status", "reason", "version",
                    "applied_issue_version", "created_at")
    list_filter = ("status",)
    inlines = [CorrectionLineInline, CorrectionEventInline]
    readonly_fields = ("original_numbers", "original_issue_month",
                       "original_issue_month_end", "version",
                       "applied_at", "withdrawn_at")


@admin.register(ExportRecord)
class ExportRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "issue", "coverage_version",
                    "correction", "created_at")
    readonly_fields = ("snapshot", "coverage_version", "correction")
