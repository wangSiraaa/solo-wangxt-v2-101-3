<template>
  <div class="panel">
    <h2>导出 / 书目上报</h2>
    <p class="muted">
      导出瞬间冻结期号覆盖与发行区间快照及版本号；之后更正、撤回都不改旧记录，
      可随时追溯导出当时的投影。
    </p>
    <div class="row">
      <label class="field"><b>发行期</b>
        <select v-model="issueId">
          <option :value="null">请选择</option>
          <option v-for="i in issues" :key="i.issue_id" :value="i.issue_id">
            {{ monthLabel(i) }} —
            {{ i.combined_numbers.map(n => `v.${n.volume||"—"}no.${n.number}`).join("+") }}
            （覆盖 v{{ i.coverage_version }}）
          </option>
        </select>
      </label>
      <label class="field"><b>备注</b>
        <input v-model="note" placeholder="如 2024 年度装订前上报" style="width:200px" />
      </label>
      <button :disabled="issueId == null" @click="doExport">冻结并导出</button>
    </div>

    <h3 v-if="records.length">导出记录（{{ records.length }}）</h3>
    <div v-for="r in records" :key="r.id" class="export-card">
      <div class="corr-head">
        <strong>导出 #{{ r.id }}</strong>
        <span class="badge version">覆盖 v{{ r.coverage_version }}</span>
        <span v-if="r.correction" class="badge corrected">
          基于更正单 #{{ r.correction }}
        </span>
        <span v-else class="badge ceased">原始发行关系</span>
        <span class="muted">{{ r.created_at?.slice(0, 19).replace("T", " ") }}</span>
        <span v-if="r.note" class="muted">{{ r.note }}</span>
      </div>
      <div class="muted">
        冻结内容：
        <span v-for="(n, i) in r.snapshot.numbers" :key="i">
          v.{{ n.volume || "—" }}no.{{ n.number }}<template v-if="n.label">（{{ n.label }}）</template>
          <span v-if="i < r.snapshot.numbers.length - 1">、</span>
        </span>
        ｜ {{ rangeOf(r.snapshot) }}
      </div>
    </div>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({
  titleId: [Number, String],
  timeline: Object,
});

const issueId = ref(null);
const note = ref("");
const records = ref([]);
const msg = ref(null);
function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 5000);
}

const issues = computed(() => {
  const out = [];
  for (const s of props.timeline?.slots || []) {
    for (const i of s.issues) {
      if (!out.some((x) => x.issue_id === i.issue_id)) out.push(i);
    }
  }
  return out;
});
function monthLabel(i) {
  const a = i.issue_month?.slice(0, 7);
  const b = i.issue_month_end?.slice(0, 7);
  return b && b !== a ? `${a}~${b}` : a;
}
function rangeOf(snap) {
  const a = snap.issue_month?.slice(0, 7);
  const b = snap.issue_month_end?.slice(0, 7);
  return b && b !== a ? `${a} ~ ${b}` : a;
}

async function load() {
  if (!props.titleId) return;
  records.value = await api.listExports(props.titleId);
}
watch(() => props.titleId, load, { immediate: true });
watch(() => props.timeline, load);

async function doExport() {
  try {
    const r = await api.createExport({
      title: props.titleId, issue: issueId.value, note: note.value,
    });
    notify(`导出 #${r.id} 已冻结为覆盖 v${r.coverage_version} 快照。`);
    note.value = "";
    await load();
  } catch (e) { notify(e.message, true); }
}
</script>
