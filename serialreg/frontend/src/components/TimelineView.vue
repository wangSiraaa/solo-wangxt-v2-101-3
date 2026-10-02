<template>
  <div class="panel">
    <h2>
      期号时间轴
      <span class="muted">
        —— {{ data.title.title }}
        <span v-if="data.title.status === 'ceased'" class="badge ceased">
          停刊于 {{ data.title.ceased_month?.slice(0, 7) }}
        </span>
      </span>
    </h2>

    <div class="timeline">
      <div
        v-for="slot in data.slots"
        :key="slot.number_id"
        class="slot"
        :class="dotClass(slot.holding_status)"
      >
        <span class="dot"></span>
        <div class="slot-card" :class="{ gap: isGap(slot.holding_status) }">
          <div class="slot-head">
            <span class="slot-no">
              v.{{ slot.volume || "—" }} no.{{ slot.number }}
            </span>
            <span class="badge" :class="badgeCls(slot.holding_status)">
              {{ statusMeta(slot.holding_status).label }}
            </span>
            <span class="slot-hint">{{ statusMeta(slot.holding_status).hint }}</span>
          </div>

          <!-- 缺号：没有发行记录，不展示入藏入口暗示 -->
          <template v-if="slot.issues.length === 0">
            <p class="empty-hint">
              该编号槽位没有发行记录（缺号）。只有登记了发行期，才能为其入藏。
            </p>
            <p
              v-for="f in slot.formerly_issued"
              :key="f.order_id"
              class="formerly-hint"
            >
              更正前曾由发行期#{{ f.issue_id }} 覆盖
              （{{ rangeText(f.issued_before.issue_month, f.issued_before.issue_month_end) }}），
              已随更正单#{{ f.order_id }} 改为
              {{ f.reassigned_to.map(x => `no.${x}`).join("+") }}。
            </p>
          </template>

          <div
            v-for="iss in slot.issues"
            :key="iss.issue_id"
            class="issue-box"
            :class="{ combined: iss.kind === 'combined' }"
          >
            <div class="slot-head">
              <span class="issue-month">{{ monthRange(iss) }}</span>
              <span v-if="iss.kind === 'combined'" class="badge combined">
                合刊 {{ iss.combined_numbers.map(n => `v.${n.volume||"—"}no.${n.number}`).join(" + ") }}
              </span>
              <span v-else class="muted">普通期</span>
              <span class="muted">v{{ iss.version }}</span>
              <span v-if="iss.has_pending_correction"
                    class="badge pending" title="存在未应用的草稿更正单，装订前需先处理">
                待决更正
              </span>
              <span v-if="iss.current_correction" class="badge corrected">
                已按更正单#{{ iss.current_correction.order_id }}更正
              </span>
            </div>

            <!-- 当前投影（更正后）与历史值（更正前）对照 -->
            <details
              v-for="c in iss.correction_history"
              :key="c.order_id"
              class="correction"
              :class="c.status"
            >
              <summary>
                <span class="badge" :class="corrBadge(c.status)">
                  {{ corrStatus[c.status] || c.status }}
                </span>
                更正单#{{ c.order_id }}：{{ c.reason }}
                <span class="muted">
                  {{ fmtSide(c.before) }} → {{ fmtSide(c.after) }}
                </span>
              </summary>
              <div class="corr-grid">
                <div class="corr-side before">
                  <b>历史值（{{ c.status === "applied" ? `v${c.version_before}` : "原覆盖" }}）</b>
                  <div>{{ kindLabel(c.before.kind) }} · {{ rangeOf(c.before) }}</div>
                  <div>{{ fmtNumbers(c.before.numbers) }}</div>
                </div>
                <div class="corr-side after">
                  <b>{{ c.status === "applied" ? `当前值（v${c.version_after}）` : "拟议值" }}</b>
                  <div>{{ kindLabel(c.after.kind) }} · {{ rangeOf(c.after) }}</div>
                  <div>{{ fmtNumbers(c.after.numbers) }}</div>
                </div>
              </div>
              <div class="muted corr-times">
                开单 {{ dt(c.created_at) }}
                <template v-if="c.applied_at">｜应用 {{ dt(c.applied_at) }}</template>
                <template v-if="c.withdrawn_at">｜撤回 {{ dt(c.withdrawn_at) }}</template>
              </div>
            </details>

            <p v-if="iss.items.length === 0" class="empty-hint">
              已发行但尚无实物 —— 此为「缺藏」，请在右侧入藏面板登记。
            </p>

            <div v-for="it in iss.items" :key="it.barcode" class="item-line">
              <code>{{ it.barcode }}</code>
              <span class="badge" :class="it.status === 'lost' ? 'missing' : 'ok'">
                {{ itemStatus[it.status] || it.status }}
              </span>
              <span class="loc">
                📍 {{ it.location || "（未排架）" }}
                <template v-if="it.bound">（装订册 {{ it.binding }}）</template>
              </span>
              <button
                v-if="!it.bound && it.status !== 'lost'"
                class="tiny ghost"
                @click="$emit('mark-lost', it.item_id)"
                title="标记丢失后，该期变为缺藏"
              >报失</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { HOLDING_STATUS, ITEM_STATUS, ISSUE_KIND } from "../status.js";

defineProps({ data: Object });
defineEmits(["mark-lost"]);
const itemStatus = ITEM_STATUS;

const isGap = (s) => s === "not_published" || s === "ceased_gap";
const corrStatus = {
  draft: "草稿", applied: "已应用", withdrawn: "已撤回",
};

function statusMeta(s) {
  return HOLDING_STATUS[s] || { label: s, cls: "gap", hint: "" };
}
function badgeCls(s) {
  return statusMeta(s).cls;
}
function dotClass(s) {
  if (s === "issued+held") return "held";
  if (s === "issued+missing") return "missing";
  return "gap";
}
function corrBadge(s) {
  if (s === "applied") return "corrected";
  if (s === "draft") return "pending";
  return "gap";
}
function monthRange(iss) {
  const a = iss.issue_month?.slice(0, 7);
  const b = iss.issue_month_end?.slice(0, 7);
  return b && b !== a ? `${a} ~ ${b}` : a;
}
function rangeOf(side) {
  return rangeText(side.issue_month, side.issue_month_end);
}
function rangeText(a, b) {
  const x = a?.slice(0, 7), y = b?.slice(0, 7);
  return y && y !== x ? `${x} ~ ${y}` : x;
}
function fmtNumbers(nums) {
  return nums.length
    ? nums.map((n) => `v.${n.volume || "—"}no.${n.number}`).join(" + ")
    : "（无编号）";
}
function fmtSide(side) {
  return `${rangeOf(side)}｜${fmtNumbers(side.numbers)}`;
}
function kindLabel(k) {
  return ISSUE_KIND[k] || k;
}
function dt(v) {
  return v ? v.replace("T", " ").slice(0, 16) : "";
}
</script>
