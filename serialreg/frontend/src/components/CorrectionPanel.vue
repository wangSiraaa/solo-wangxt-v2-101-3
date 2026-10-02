<template>
  <div class="panel">
    <h2>发行更正单</h2>

    <!-- 开单 -->
    <div class="row">
      <label class="field"><b>更正发行期</b>
        <select v-model="form.issue_id" @change="onIssueChange">
          <option :value="null">请选择发行期</option>
          <option v-for="i in allIssues" :key="i.issue_id" :value="i.issue_id">
            {{ monthLabel(i) }} —
            {{ i.combined_numbers.map(n => `v.${n.volume||"—"}no.${n.number}`).join("+") }}
            （v{{ i.version }}）
          </option>
        </select>
      </label>
    </div>
    <div class="row">
      <label class="field"><b>拟议类型</b>
        <select v-model="form.kind">
          <option value="regular">普通期（1 个期号）</option>
          <option value="combined">两期合刊（≥2 个期号）</option>
        </select>
      </label>
      <label class="field"><b>拟议起始月</b>
        <input v-model="form.issue_month" type="month" />
      </label>
      <label class="field"><b>拟议截止月</b>
        <input v-model="form.issue_month_end" type="month" />
      </label>
    </div>
    <div v-if="form.issue_id" class="checks">
      <span class="muted">拟议覆盖编号（当前编号已勾选）：</span><br />
      <label v-for="s in timeline.slots" :key="s.number_id">
        <input type="checkbox" :value="s.number_id" v-model="form.number_ids" />
        v.{{ s.volume || "—" }} no.{{ s.number }}
      </label>
    </div>
    <div class="row" style="margin-top:6px">
      <label class="field" style="flex:1"><b>更正原因</b>
        <input v-model="form.reason" placeholder="如：编辑部通知实际为 4-5 月合刊" />
      </label>
      <button :disabled="!form.issue_id" @click="submitDraft">
        开立更正单（草稿）
      </button>
    </div>
    <p class="muted">
      更正只改编号覆盖与发行区间的投影，实物、条码、装订位置不断链；
      原关系封存为历史快照，应用后时间轴与定位使用新值，撤回可还原。
    </p>

    <!-- 单据列表 -->
    <h3 v-if="orders.length">更正单（{{ orders.length }}）</h3>
    <div v-for="o in orders" :key="o.id" class="correction-card">
      <div class="row">
        <span class="badge" :class="orderBadge(o.status)">
          {{ orderStatus[o.status] || o.status }}
        </span>
        <strong>#{{ o.id }}</strong>
        <span class="muted">{{ o.reason }}</span>
        <button v-if="o.status === 'draft'" class="tiny"
                @click="apply(o.id)">应用</button>
        <button v-if="o.status !== 'withdrawn'" class="tiny ghost"
                @click="withdraw(o.id)">撤回</button>
        <button v-if="o.status === 'draft'" class="tiny danger"
                @click="remove(o.id)">删除</button>
      </div>
      <div v-for="ln in o.lines" :key="ln.issue_id" class="corr-mini">
        <div class="corr-side before">
          原值{{ ln.version_before ? `（v${ln.version_before}）` : "" }}：
          {{ sideText(ln.before) }}
        </div>
        <div class="corr-side after">
          {{ o.status === "draft" ? "拟议" : `新值（v${ln.version_after}）` }}：
          {{ sideText(ln.after) }}
        </div>
      </div>
    </div>

    <!-- 导出证据 -->
    <h3 style="margin-top:12px">导出证据快照</h3>
    <div class="row">
      <label class="field"><b>按条码冻结</b>
        <input v-model="freezeBarcode" placeholder="CB-34" style="width:130px" />
      </label>
      <button class="ghost" @click="freeze">冻结当前投影</button>
    </div>
    <div v-for="e in exports" :key="e.id" class="export-line">
      <code>#{{ e.id }}</code>
      <span class="muted">{{ e.created_at.replace("T", " ").slice(0, 16) }}</span>
      <span>issue#{{ e.issue }} v{{ e.issue_version }}</span>
      <span v-if="e.barcode"><code>{{ e.barcode }}</code></span>
      <span>{{ rangeText(e.month_start, e.month_end) }}｜
        {{ (e.numbers_json || []).map(n => `v.${n.volume||"—"}no.${n.number}`).join("+") }}
      </span>
      <span class="loc">📍 {{ e.location || "—" }}</span>
    </div>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({
  titleId: [Number, String],
  timeline: Object,
});
const emit = defineEmits(["changed"]);

const orderStatus = { draft: "草稿", applied: "已应用", withdrawn: "已撤回" };
const orders = ref([]);
const exports = ref([]);
const msg = ref(null);
const freezeBarcode = ref("");

const form = reactive({
  issue_id: null, kind: "regular", issue_month: "", issue_month_end: "",
  number_ids: [], reason: "",
});

function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 6000);
}

const allIssues = computed(() => {
  const out = [];
  for (const s of props.timeline?.slots || []) {
    for (const i of s.issues) if (!out.some((x) => x.issue_id === i.issue_id)) out.push(i);
  }
  return out;
});

function monthLabel(i) {
  const a = i.issue_month?.slice(0, 7);
  const b = i.issue_month_end?.slice(0, 7);
  return b && b !== a ? `${a}~${b}` : a;
}
function m(v) {
  return v ? v.slice(0, 7) : "";
}
function rangeText(a, b) {
  const x = a?.slice(0, 7), y = b?.slice(0, 7);
  return y && y !== x ? `${x}~${y}` : x;
}
function sideText(side) {
  const nums = (side.numbers || [])
    .map((n) => `v.${n.volume || "—"}no.${n.number}`).join("+");
  return `${rangeText(side.issue_month, side.issue_month_end)}｜${nums}`;
}
function orderBadge(s) {
  if (s === "applied") return "corrected";
  if (s === "draft") return "pending";
  return "gap";
}

function onIssueChange() {
  const iss = allIssues.value.find((i) => i.issue_id === form.issue_id);
  if (!iss) return;
  form.kind = iss.kind;
  form.issue_month = m(iss.issue_month);
  form.issue_month_end = m(iss.issue_month_end);
  // 当前覆盖的编号 id：从时间轴槽位反查
  const wanted = new Set(iss.combined_numbers.map((n) => `${n.volume}::${n.number}`));
  form.number_ids = (props.timeline.slots || [])
    .filter((s) => wanted.has(`${s.volume}::${s.number}`))
    .map((s) => s.number_id);
}

async function load() {
  try {
    const [os, es] = await Promise.all([
      api.listCorrections(props.titleId),
      api.listExports(props.titleId),
    ]);
    orders.value = os;
    exports.value = es;
  } catch (e) { /* 列表非关键路径 */ }
}
watch(() => props.titleId, load, { immediate: true });

async function reload(msgText) {
  await load();
  emit("changed");
  if (msgText) notify(msgText);
}

async function submitDraft() {
  const nums = form.number_ids;
  if (form.kind === "combined" && nums.length < 2)
    return notify("两期合刊必须覆盖至少两个期号。", true);
  if (form.kind === "regular" && nums.length !== 1)
    return notify("普通期只能覆盖一个期号。", true);
  if (!form.issue_month)
    return notify("请填写拟议起始月。", true);
  if (!form.reason)
    return notify("请填写更正原因。", true);
  try {
    const created = await api.draftCorrection({
      title: props.titleId,
      reason: form.reason,
      client_ref: `web-${props.titleId}-${form.issue_id}-${Date.now()}`,
      lines: [{
        issue: form.issue_id,
        kind: form.kind,
        issue_month: `${form.issue_month}-01`,
        issue_month_end: form.issue_month_end ? `${form.issue_month_end}-01` : null,
        number_ids: nums,
      }],
    });
    form.issue_id = null;
    form.reason = ""; form.number_ids = [];
    await reload(`草稿更正单 #${created.id} 已开立，确认无误后请应用。`);
  } catch (e) { notify(e.message, true); }
}

async function apply(id) {
  try {
    const r = await api.applyCorrection(id);
    await reload(r.changed ? `更正单 #${id} 已应用，时间轴与定位已使用新投影。`
                           : `更正单 #${id} 此前已应用（幂等，未重复变更）。`);
  } catch (e) { notify(e.message, true); }
}

async function withdraw(id) {
  try {
    const r = await api.withdrawCorrection(id);
    await reload(r.changed ? `更正单 #${id} 已撤回，投影已还原/草稿已关闭。`
                           : `更正单 #${id} 此前已撤回（幂等）。`);
  } catch (e) { notify(e.message, true); }
}

async function remove(id) {
  try {
    await api.deleteCorrection(id);
    await reload(`草稿 #${id} 已删除。`);
  } catch (e) { notify(e.message, true); }
}

async function freeze() {
  if (!freezeBarcode.value) return notify("请输入要冻结导出的条码。", true);
  try {
    const e = await api.freezeExport({ barcode: freezeBarcode.value });
    freezeBarcode.value = "";
    await reload(`已冻结导出快照 #${e.id}（issue v${e.issue_version}），后续更正不影响该记录。`);
  } catch (err) { notify(err.message, true); }
}
</script>
