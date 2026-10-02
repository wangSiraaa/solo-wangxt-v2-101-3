const BASE = "/api";

async function request(path, options = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    const detail =
      typeof data === "object" && data
        ? Object.entries(data)
            .map(([k, v]) => `${k}: ${[].concat(v).join("；")}`)
            .join("｜")
        : String(data);
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return data;
}

export const api = {
  listTitles: () => request("/titles/"),
  createTitle: (payload) =>
    request("/titles/", { method: "POST", body: JSON.stringify(payload) }),
  updateTitle: (id, payload) =>
    request(`/titles/${id}/`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  timeline: (titleId) => request(`/timeline/?title=${titleId}`),
  listNumbers: (titleId) => request(`/numbers/?title=${titleId}`),

  createNumber: (payload) =>
    request("/numbers/", { method: "POST", body: JSON.stringify(payload) }),
  createIssue: (payload) =>
    request("/issues/", { method: "POST", body: JSON.stringify(payload) }),
  createItem: (payload) =>
    request("/items/", { method: "POST", body: JSON.stringify(payload) }),
  setItemStatus: (id, status) =>
    request(`/items/${id}/`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  locate: (params) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null),
    ).toString();
    return request(`/items/locate/?${qs}`);
  },

  listBindings: (titleId) =>
    request(titleId ? `/bindings/?title=${titleId}` : "/bindings/"),
  bind: (payload) =>
    request("/bindings/", { method: "POST", body: JSON.stringify(payload) }),
  unbind: (bindingId) =>
    request("/bindings/unbind/", {
      method: "POST",
      body: JSON.stringify({ binding_id: bindingId }),
    }),

  // 发行更正单
  listCorrections: (titleId) =>
    request(`/corrections/?title=${titleId}`),
  draftCorrection: (payload) =>
    request("/corrections/", { method: "POST", body: JSON.stringify(payload) }),
  applyCorrection: (id) =>
    request(`/corrections/${id}/apply/`, { method: "POST", body: "{}" }),
  withdrawCorrection: (id) =>
    request(`/corrections/${id}/withdraw/`, { method: "POST", body: "{}" }),
  deleteCorrection: (id) =>
    request(`/corrections/${id}/`, { method: "DELETE" }),

  // 导出快照
  listExports: (titleId) =>
    request(titleId ? `/exports/?title=${titleId}` : "/exports/"),
  freezeExport: (payload) =>
    request("/exports/freeze/", {
      method: "POST", body: JSON.stringify(payload),
    }),
};
