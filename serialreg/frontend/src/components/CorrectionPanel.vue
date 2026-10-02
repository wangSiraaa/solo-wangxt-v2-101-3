<template>
  <div class="panel">
    <h2>发行更正单</h2>
    <p class="muted">
      更正只改「覆盖投影」：期号定位、时间轴、条码反查用更正后的值；
      已装订记录、旧定位证据与导出快照按版本保留，不静默抹历史。
    </p>

    <h3>① 选择要更正的发行期并填写拟议覆盖</h3>
    <div class="row">
      <label class="field"><b>发行期</b>
        <select v-model="form.issue_id">
          <option :value="null">请选择</option>
          <option v-for="i in issues" :key="i.issue_id" :value="i.issue_id">
            {{ monthLabel(i) }} —
            {{ i.combined_numbers.map(n => `v.${n.volume||"—"}no.${n.number}`).join("+") }}
            {{ i.kind === "combined" ? "（合刊）" : "" }}
            <template v-if="i.active_correction_id">[已更正 #{{ i.active_correction_id }}]</template>
          </option>
        </select>
      </label>
      <label class="field"><b>拟议发行年月</b>
        <input v-model="form.start" type="month" />
      </label>
      <label class="field"><b>拟议截止年月</b>
        <input v-model="form.end" type="month" />
      </label>
    </div>

    <div v-if="selectedIssue" class="corr-lines">
      <p class="muted">
        原覆盖的每个期号一行；拟议期号可改成空闲槽位。多条行作为同一张单、
        一个事务整体校验，冲突编号会整单拒绝。
      </p>
      <div v-for="row in form.rows" :key="row.original.number_id" class="row corr-row">
        <span class="badge ceased">
          v.{{ row.original.volume || "—" }} no.{{ row.original.number }}
        </span>
        <span>→</span>
        <select v-model="row.proposed_number_id">
          <option :value="null">（撤销覆盖）</option>
          <option
            v-for="s in candidateSlots"
            :key="s.number_id"
            :value="s.number_id"
          >
            v.{{ s.volume || "—" }} no.{{ s.number }}
            {{ s.issues.length ? "（已被占用）" : "（空闲）" }}
          </option>
        </select>
        <label class="field" style="flex-direction:row;align-items:center;gap:4px">
          <b>封面标识</b>
          <input v-model="row.label" placeholder="no.5-6" style="width:110px" />
        </label>
      </div>
    </div>

    <div class="row" style="margin-top:8px">
      <label class="field"><b>更正原因</b>
        <input v-model="form.reason" placeholder="如：编辑部勘误，实际为 5-6 月合刊"
               style="width:260px" />
      </label>
      <button :disabled="!canSubmit" @click="submitDraft">
        {{ editingDraftId ? `修改草稿 #${editingDraftId}` : "建立更正单（草稿）" }}
      </button>
      <button v-if="editingDraftId" class="ghost" @click="resetForm">放弃编辑</button>
    </div>

    <h3 v-if="corrections.length">② 更正单列表（应用 / 撤回，均幂等）</h3>
    <div v-for="c in corrections" :key="c.id" class="correction-card" :class="c.status">
      <div class="corr-head">
        <span class="badge" :class="statusCls(c.status)">{{ c.status_label }} #{{ c.id }}</span>
        <span class="muted">{{ c.reason }}</span>
        <span class="muted">单版本 v{{ c.version }}</span>
        <span v-if="c.applied_issue_version" class="muted">
          发行覆盖 v{{ c.applied_issue_version }}
        </span>
      </div>
      <div class="corr-diff">
        <span v-for="(ln, i) in c.lines_detail" :key="i">
          <template v-if="ln.original">
            v.{{ ln.original.volume || "—" }}no.{{ ln.original.number }}
          </template>
          <template v-else>—</template>
          →
          <template v-if="ln.proposed">
            <b>v.{{ ln.proposed.volume || "—" }}no.{{ ln.proposed.number }}</b>
          </template>
          <template v-else><b>（撤销）</b></template>
          <span v-if="i < c.lines_detail.length - 1">；</span>
        </span>
        <br />
        <span class="muted">
          {{ monthLabel2(c.original_issue_month, c.original_issue_month_end) }}
          → <b>{{ monthLabel2(c.proposed_issue_month, c.proposed_issue_month_end) }}</b>
        </span>
      </div>
      <div class="row" style="margin-top:6px">
        <button
          v-if="c.status === 'draft'"
          class="tiny"
          @click="apply(c)"
        >应用（带版本 v{{ c.version }}）</button>
        <button
          v-if="c.status === 'draft'"
          class="tiny ghost"
          @click="editDraft(c)"
        >修改</button>
        <button
          v-if="c.status === 'draft'"
          class="tiny danger"
          @click="remove(c)"
        >删除草稿</button>
        <button
          v-if="c.status === 'applied'"
          class="tiny danger"
          @click="withdraw(c)"
        >撤回（还原原覆盖，带版本 v{{ c.version }}）</button>
        <span v-if="c.status === 'withdrawn'" class="muted">
          已撤回并还原；历史快照保留，重复撤回不会再改关系。
        </span>
      </div>
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

const msg = ref(null);
function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 6000);
}

const form = reactive({
  issue_id: null, start: "", end: "", reason: "", rows: [],
});
const editingDraftId = ref(null);

// 时间轴里去重后的发行期列表
const issues = computed(() => {
  const out = [];
  for (const s of props.timeline?.slots || []) {
    for (const i of s.issues) {
      if (!out.some((x) => x.issue_id === i.issue_id)) out.push(i);
    }
  }
  return out;
});

const selectedIssue = computed(() =>
  issues.value.find((i) => i.issue_id === form.issue_id) || null,
);

// 候选拟议槽位：全部槽位（含当前占用——后端会对占用冲突整单报错）
const candidateSlots = computed(() => props.timeline?.slots || []);

watch(() => form.issue_id, (id) => {
  if (id == null) return;
  const iss = issues.value.find((x) => x.issue_id === id);
  if (!iss) return;
  // 一个 issue 只在第一个槽位带完整数据，但 combined_numbers 各处一致
  form.start = iss.issue_month?.slice(0, 7) || "";
  form.end = iss.issue_month_end?.slice(0, 7) || "";
  form.rows = iss.combined_numbers.map((n, idx) => ({
    original: {
      number_id: n.number_id, volume: n.volume, number: n.number,
    },
    proposed_number_id: n.number_id,
    label: idx === 0 && iss.kind === "combined"
      ? defaultLabel(iss.combined_numbers) : "",
  }));
});

function defaultLabel(nums) {
  return "no." + nums.map((n) => n.number).join("-");
}

const canSubmit = computed(() => {
  if (!form.issue_id || !form.start || !form.reason) return false;
  if (!form.rows.length) return false;
  const proposed = form.rows.map((r) => r.proposed_number_id);
  if (proposed.some((v) => v == null)) return false;
  return new Set(proposed).size === proposed.length;
});

function monthLabel(i) {
  const a = i.issue_month?.slice(0, 7);
  const b = i.issue_month_end?.slice(0, 7);
  return b && b !== a ? `${a}~${b}` : a;
}
function monthLabel2(a, b) {
  const x = a?.slice(0, 7);
  const y = b?.slice(0, 7);
  return y && y !== x ? `${x}~${y}` : (x || "—");
}
function statusCls(s) {
  if (s === "applied") return "corrected";
  if (s === "withdrawn") return "ceased";
  return "gap";
}

const corrections = ref([]);
async function loadCorrections() {
  if (!props.titleId) return;
  try {
    corrections.value = await api.listCorrections(props.titleId);
  } catch (e) { /* 非关键路径 */ }
}
watch(() => props.titleId, loadCorrections, { immediate: true });
watch(() => props.timeline, loadCorrections);

function payloadFor() {
  return {
    issue: form.issue_id,
    reason: form.reason,
    proposed_issue_month: `${form.start}-01`,
    proposed_issue_month_end: form.end ? `${form.end}-01` : null,
    lines: form.rows.map((r) => ({
      original_number_id: r.original.number_id,
      proposed_number_id: r.proposed_number_id,
      proposed_label: r.label || "",
    })),
  };
}

async function submitDraft() {
  try {
    if (editingDraftId.value) {
      await api.updateCorrection(editingDraftId.value, payloadFor());
      notify(`草稿 #${editingDraftId.value} 已修改（版本推进，防乱序）。`);
    } else {
      const created = await api.createCorrection(payloadFor());
      notify(`更正单草稿 #${created.id} 已建立。`);
    }
    resetForm();
    await loadCorrections();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

function resetForm() {
  editingDraftId.value = null;
  form.issue_id = null; form.start = ""; form.end = "";
  form.reason = ""; form.rows = [];
}

function editDraft(c) {
  editingDraftId.value = c.id;
  form.issue_id = c.issue;
  form.start = c.proposed_issue_month?.slice(0, 7) || "";
  form.end = c.proposed_issue_month_end?.slice(0, 7) || "";
  form.reason = c.reason;
  form.rows = c.lines_detail.map((ln) => ({
    original: ln.original,
    proposed_number_id: ln.proposed?.number_id ?? null,
    label: ln.proposed_label || "",
  }));
}

async function apply(c) {
  try {
    const r = await api.applyCorrection(c.id, c.version);
    notify(
      `更正单 #${c.id} 已应用：新时间轴/定位/条码反查使用 v${r.applied_issue_version} 投影。`,
    );
    await loadCorrections();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function withdraw(c) {
  try {
    const r = await api.withdrawCorrection(c.id, c.version);
    notify(
      `更正单 #${c.id} 已撤回：覆盖还原至 v${r.withdrawn_issue_version}，历史快照保留。`,
    );
    await loadCorrections();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function remove(c) {
  try {
    await api.deleteCorrection(c.id);
    notify(`草稿 #${c.id} 已删除。`);
    if (editingDraftId.value === c.id) resetForm();
    await loadCorrections();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}
</script>
