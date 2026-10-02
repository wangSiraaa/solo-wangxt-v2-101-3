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
              <span class="badge version" title="覆盖关系版本：每次应用/撤回更正 +1">
                覆盖 v{{ iss.coverage_version }}
              </span>
              <span v-if="iss.active_correction_id" class="badge corrected">
                已由更正单 #{{ iss.active_correction_id }} 更正
              </span>
              <span v-if="iss.pending_correction_id" class="badge gap"
                    title="该发行有未应用的更正草稿；在此期间入藏/装订会被拒绝">
                未决更正单 #{{ iss.pending_correction_id }}（入藏/装订暂停）
              </span>
              <button
                v-if="iss.corrections.length"
                class="tiny ghost"
                @click="toggleHistory(iss.issue_id)"
              >
                {{ openHistory.has(iss.issue_id) ? "收起更正记录" : `更正记录（${iss.corrections.length}）` }}
              </button>
            </div>

            <!-- 更正历史：当前值 vs 历史值，永远展示应用前快照 -->
            <div v-if="openHistory.has(iss.issue_id)" class="correction-history">
              <div
                v-for="c in iss.corrections"
                :key="c.id"
                class="correction-card"
                :class="c.status"
              >
                <div class="corr-head">
                  <span class="badge" :class="corrBadge(c.status)">
                    {{ c.status_label }} #{{ c.id }}
                  </span>
                  <span class="muted">{{ c.reason }}</span>
                  <span v-if="c.applied_issue_version" class="muted">
                    应用至 v{{ c.applied_issue_version }}
                  </span>
                  <span v-if="c.withdrawn_issue_version" class="muted">
                    撤回至 v{{ c.withdrawn_issue_version }}
                  </span>
                </div>
                <table class="corr-table">
                  <thead>
                    <tr><th></th><th>历史值（应用前）</th><th>当前/拟议值</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(ln, i) in c.lines" :key="i">
                      <td class="muted">期号</td>
                      <td :class="{ 'old-val': c.status === 'applied' }">
                        {{ ln.original ? `v.${ln.original.volume || "—"} no.${ln.original.number}` : "—" }}
                      </td>
                      <td :class="{ 'new-val': c.status === 'applied' }">
                        {{ ln.proposed ? `v.${ln.proposed.volume || "—"} no.${ln.proposed.number}` : "（撤销）" }}
                      </td>
                    </tr>
                    <tr>
                      <td class="muted">区间</td>
                      <td :class="{ 'old-val': c.status === 'applied' }">
                        {{ monthPair(c.original_issue_month, c.original_issue_month_end) }}
                      </td>
                      <td :class="{ 'new-val': c.status === 'applied' }">
                        {{ monthPair(c.proposed_issue_month, c.proposed_issue_month_end) }}
                      </td>
                    </tr>
                  </tbody>
                </table>
                <details>
                  <summary class="muted">审计事件（{{ c.events.length }}）</summary>
                  <ul class="event-list">
                    <li v-for="e in c.events" :key="e.created_at + e.action">
                      <code>{{ e.created_at?.slice(0, 19).replace("T", " ") }}</code>
                      <b>{{ e.action_label }}</b>
                      <span class="muted">{{ e.detail }}</span>
                    </li>
                  </ul>
                </details>
              </div>
            </div>

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
import { reactive } from "vue";
import { HOLDING_STATUS, ITEM_STATUS } from "../status.js";

defineProps({ data: Object });
defineEmits(["mark-lost"]);
const itemStatus = ITEM_STATUS;

// 默认展开已应用更正的历史卡片，便于一眼看到当前值/历史值
const openHistory = reactive(new Set());

function toggleHistory(id) {
  if (openHistory.has(id)) openHistory.delete(id);
  else openHistory.add(id);
}

const isGap = (s) => s === "not_published" || s === "ceased_gap";

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
  if (s === "withdrawn") return "ceased";
  return "gap";
}
function monthRange(iss) {
  return monthPair(iss.issue_month, iss.issue_month_end);
}
function monthPair(a, b) {
  const x = a?.slice(0, 7);
  const y = b?.slice(0, 7);
  if (!x) return "—";
  return y && y !== x ? `${x} ~ ${y}` : x;
}
</script>
