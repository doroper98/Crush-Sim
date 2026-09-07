/* app.js - 안내형 그래프 작업실 (UI_001 §3-§13, UI_002 §3 WP2).

   빌드 없음, CDN 없음, 프레임워크 없음. 책임은 UI_001 §13의 표를 따른다:

     Api            요청·오류 정규화·중복 요청 방지
     Caps           서버 capability(목적·재료·프리셋·업로드 한계)의 유일한 출처
     GraphModel     노드·연결·개정 + 명령 기반 undo/redo
     GuidedSetup    같은 GraphModel을 설계 언어로 편집(네 단계 안내)
     GraphView      그래프의 표시와 사용자 명령 전달
     Inspector      단계/노드 속성 패널
     RunStore       서버 실행 상태(편집본과 분리)
     ResultPresenter 서버가 만든 판단 문장·지표의 표시

   브라우저가 하지 않는 것: 물리, 파일 파싱, 게이트 한계, 결과 문장 생성,
   실행 이름·건수·예상 시간(전부 /api/preflights 응답).
   사용자 문자열(파일명·부품명·로그)은 textContent로만 넣는다(U42). */
"use strict";

/* ------------------------------------------------------------------ 도구 */

const $ = (id) => document.getElementById(id);

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const key in attrs || {}) {
    const value = attrs[key];
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "dataset") for (const d in value) node.dataset[d] = value[d];
    else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** 유효숫자 약 3자리. 작은 잔여 두께처럼 구분에 필요한 자릿수는 지킨다(§10.4). */
function num(value, digits) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) return "–";
  const v = Number(value);
  if (digits !== undefined) return v.toFixed(digits);
  if (v === 0) return "0";
  const mag = Math.abs(v);
  if (mag >= 1000) return v.toFixed(0);
  if (mag >= 100) return v.toFixed(1);
  if (mag >= 1) return v.toFixed(2);
  return v.toPrecision(3).replace(/0+$/, "").replace(/\.$/, "");
}

function timeOfDay(date) {
  const d = date || new Date();
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0")
    + ":" + String(d.getSeconds()).padStart(2, "0");
}

function announce(message) {
  const region = $("liveRegion");
  if (region) region.textContent = message;
}

function isTyping(event) {
  const t = event.target;
  if (!t) return false;
  const tag = (t.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || t.isContentEditable;
}

function stableStringify(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(stableStringify).join(",") + "]";
  return "{" + Object.keys(value).sort().map((k) => JSON.stringify(k) + ":" + stableStringify(value[k])).join(",") + "}";
}

/* ------------------------------------------------------------------ Api */

const Api = {
  _inflight: new Map(),

  async request(path, options) {
    const opts = options || {};
    const key = (opts.method || "GET") + " " + path;
    /* 같은 GET을 중첩해서 보내지 않는다(5초 폴링이 겹치면 서버가 아니라
       브라우저가 밀린다 - UI_001 §9.2). */
    if (!opts.method || opts.method === "GET") {
      const pending = this._inflight.get(key);
      if (pending) return pending;
    }
    const promise = (async () => {
      let response;
      try {
        response = await fetch(path, opts);
      } catch (err) {
        throw { code: "NETWORK", message: "서버에 연결하지 못했습니다.", severity: "block", detail: String(err), network: true };
      }
      let body = null;
      const text = await response.text();
      if (text) {
        try { body = JSON.parse(text); } catch (err) { body = null; }
      }
      if (!response.ok) {
        const error = body && body.code
          ? body
          : { code: "HTTP_" + response.status, message: "요청을 처리하지 못했습니다.", severity: "block", detail: text.slice(0, 400) };
        error.status = response.status;
        throw error;
      }
      return body;
    })();
    if (!opts.method || opts.method === "GET") {
      this._inflight.set(key, promise);
      promise.catch(() => {}).then(() => this._inflight.delete(key));
    }
    return promise;
  },

  get(path) { return this.request(path); },
  post(path, payload) {
    return this.request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
  },
  put(path, payload) {
    return this.request(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
  },
  upload(path, file) {
    const form = new FormData();
    form.append("file", file);
    return this.request(path, { method: "POST", body: form });
  }
};

/* --------------------------------------------------- CapabilityRegistry */

const Caps = {
  data: null,
  async load() {
    this.data = await Api.get("/api/capabilities");
    return this.data;
  },
  purposes() { return (this.data && this.data.purposes) || []; },
  purpose(id) { return this.purposes().find((p) => p.id === id) || null; },
  materials() { return ((this.data && this.data.materials) || []).filter((m) => !m.error); },
  material(key) { return this.materials().find((m) => m.key === key) || null; },
  threads() { return (this.data && this.data.threads) || 4; },
  upload() { return (this.data && this.data.upload) || { max_bytes: 0, extensions: [] }; },
  defaultPreset(purposeId) {
    const purpose = this.purpose(purposeId);
    return ((purpose && purpose.mesh_presets) || []).find((p) => p.default) || null;
  },
  presets(purposeId) {
    const purpose = this.purpose(purposeId);
    return (purpose && purpose.mesh_presets) || [];
  }
};

/* ------------------------------------------------------------ 노드 정의 */

/* 그래프의 의미는 기존 편집기와 같다: 캔 → 캡 → 벤트 체인, 다중 입력 = 곱집합
   스윕. 서버 컴파일러(graphc.py)가 정본이라 여기서 값을 바꾸면 안 된다. */
const NODE_TYPES = {
  geometry: {
    title: "형상", out: "geom", ins: [], group: "physical",
    fields: [
      { p: "kind", label: "종류", type: "select", opts: ["parametric_can", "box_can", "step"], redraw: true },
      { p: "step_path", label: "STEP 경로 (고급)", type: "text", advanced: true, show: (n) => n.params.kind === "step" },
      { p: "radius", label: "반경", unit: "mm", type: "num", show: (n) => n.params.kind === "parametric_can" },
      { p: "width", label: "폭", unit: "mm", type: "num", show: (n) => n.params.kind === "box_can" },
      { p: "depth", label: "깊이", unit: "mm", type: "num", show: (n) => n.params.kind === "box_can" },
      { p: "height", label: "높이", unit: "mm", type: "num", show: (n) => n.params.kind !== "step" },
      { p: "thickness", label: "판 두께", unit: "mm", type: "num" },
      { p: "closed_bottom", label: "바닥 닫힘", type: "bool" }
    ],
    defaults: { kind: "parametric_can", radius: 33, height: 115, thickness: 0.1 },
    summary: (n) => {
      const p = n.params;
      if (p.kind === "step") return ["STEP 형상", p.thickness ? "t " + num(p.thickness) + " mm" : "두께 확인 필요"];
      if (p.kind === "box_can") return [num(p.width) + "×" + num(p.depth) + "×" + num(p.height) + " mm", "t " + num(p.thickness) + " mm"];
      return ["Ø" + num(p.radius * 2) + " × " + num(p.height) + " mm", "t " + num(p.thickness) + " mm"];
    }
  },
  cap: {
    title: "캡 (용접 뚜껑)", out: "geom", ins: [{ port: "geom", label: "캔" }], group: "physical",
    fields: [{ p: "note", label: "연결 가정", type: "text" }],
    defaults: { note: "용접 = 절점 공유" },
    summary: (n) => [String(n.params.note || "용접 = 절점 공유")]
  },
  vent: {
    title: "벤트 (포일 + 스코어)", out: "geom", ins: [{ port: "geom", label: "캡" }], group: "physical",
    fields: [
      { p: "length", label: "벤트 길이", unit: "mm", type: "num" },
      { p: "width", label: "벤트 폭", unit: "mm", type: "num" },
      { p: "band", label: "스코어 폭", unit: "mm", type: "num" },
      { p: "membrane_thickness", label: "포일 두께", unit: "mm", type: "num" },
      { p: "score_thickness", label: "스코어 잔여 두께", unit: "mm", type: "num", help: "홈을 가공하고 남은 두께입니다." },
      { p: "pattern", label: "스코어 패턴", type: "select", opts: ["perimeter", "petal_x"] },
      { p: "material", label: "포일 재료", type: "material" },
      { p: "eps_p_max", label: "포일 파단변형률", type: "num", advanced: true }
    ],
    defaults: {
      length: 30, width: 7, band: 1.0, membrane_thickness: 0.1, score_thickness: 0.03,
      pattern: "petal_x", material: "al1050_foil", eps_p_max: 0.4
    },
    summary: (n) => [String(n.params.pattern || "-"), "잔여 " + num(n.params.score_thickness) + " mm"]
  },
  mesh: {
    title: "메쉬", out: "mesh", ins: [{ port: "geom", label: "형상" }], group: "analysis",
    fields: [
      { p: "target_size", label: "요소 크기", unit: "mm", type: "num" },
      { p: "vent_size", label: "벤트 세분", unit: "mm", type: "num" },
      { p: "imperfection_mm", label: "불완전성", unit: "mm", type: "num", advanced: true },
      { p: "tag", label: "브랜치 태그", type: "text", advanced: true }
    ],
    defaults: { target_size: 1.5 },
    summary: (n) => [num(n.params.target_size) + " mm", n.params.vent_size ? "벤트 " + num(n.params.vent_size) + " mm" : "벤트 세분 자동"]
  },
  material: {
    title: "재료", out: "mat", ins: [], group: "physical",
    fields: [
      { p: "key", label: "재료 카드", type: "material" },
      { p: "eps_p_max", label: "파단 변형률", type: "num", advanced: true },
      { p: "tag", label: "브랜치 태그", type: "text", advanced: true }
    ],
    defaults: { key: "aluminum_3003" },
    summary: (n) => {
      const card = Caps.material(n.params.key);
      return [card ? card.name : String(n.params.key || "재료 확인 필요"), card ? (card.verified ? "검증됨" : "검증 미확인") : "확인 필요"];
    }
  },
  loading: {
    title: "하중 — 공구", out: "load", ins: [], group: "physical",
    fields: [
      { p: "tool", label: "누르는 도구", type: "select", opts: ["platen", "jig_plane", "v_block", "indenter", "cylinder", "bead_roller"], redraw: true },
      { p: "direction", label: "방향", type: "vec", advanced: true },
      { p: "stroke", label: "누르는 거리", unit: "mm", type: "num" },
      { p: "velocity", label: "속도", unit: "m/s", type: "num", advanced: true },
      { p: "tool_gap", label: "초기 갭", unit: "mm", type: "num", advanced: true },
      { p: "indenter_radius", label: "공구 반경", unit: "mm", type: "num", show: (n) => ["indenter", "cylinder", "bead_roller"].includes(n.params.tool) },
      { p: "tool_size", label: "공구 크기", unit: "mm", type: "num", advanced: true },
      { p: "height_frac", label: "높이 위치 0-1", type: "num", advanced: true },
      { p: "support", label: "지지 방식", type: "select", opts: ["", "jig_plane", "v_block", "bead_arbor"] },
      { p: "clamp_can_base", label: "바닥 고정", type: "bool" },
      { p: "tag", label: "브랜치 태그", type: "text", advanced: true }
    ],
    defaults: { tool: "platen", direction: [0, 0, -1], stroke: 40, velocity: 2, tool_gap: 0.5 },
    summary: (n) => [String(n.params.tool || ""), directionSentence(n.params.direction, n.params.stroke)]
  },
  pressure: {
    title: "하중 — 내압", out: "load", ins: [], group: "physical",
    fields: [
      { p: "peak", label: "최대 가압 압력", unit: "MPa", type: "num" },
      { p: "rise", label: "가압 램프", unit: "s", type: "num", advanced: true },
      { p: "hold", label: "유지", unit: "s", type: "num", advanced: true },
      { p: "clamp_can_base", label: "바닥 고정", type: "bool" },
      { p: "brace_walls", label: "넓은 면 구속 (모듈)", type: "bool" },
      { p: "tag", label: "브랜치 태그", type: "text", advanced: true }
    ],
    defaults: { peak: 3.0, rise: 0.003, hold: 0.001, clamp_can_base: true },
    summary: (n) => ["내압 최대 " + num(n.params.peak) + " MPa", n.params.brace_walls ? "넓은 면 구속" : "단독 셀"]
  },
  contact: {
    title: "접촉", out: "contact", ins: [], group: "analysis",
    fields: [{ p: "friction", label: "마찰 계수", type: "num" }],
    defaults: { friction: 0.15 },
    summary: (n) => ["마찰 " + num(n.params.friction)]
  },
  solver: {
    title: "솔버 · 실행", out: "runs", group: "analysis",
    ins: [
      { port: "mesh", label: "메쉬 (다중 = 스윕)", multi: true },
      { port: "mat", label: "재료 (다중 = 스윕)", multi: true },
      { port: "load", label: "하중 (다중 = 스윕)", multi: true },
      { port: "contact", label: "접촉 (선택)" }
    ],
    fields: [
      { p: "prefix", label: "검토 이름", type: "text" },
      { p: "load_case", label: "LC", type: "select", opts: ["LC-1", "LC-2", "LC-3"], advanced: true },
      { p: "mesh_preset", label: "메쉬 프리셋", type: "preset" },
      { p: "threads", label: "스레드", type: "num", advanced: true },
      { p: "animation_frames", label: "프레임", type: "num", advanced: true },
      { p: "render", label: "애니메이션", type: "bool", advanced: true },
      { p: "report", label: "리포트", type: "bool", advanced: true }
    ],
    defaults: { prefix: "graph_case", load_case: "LC-1", threads: 4, animation_frames: 25, render: true, report: true },
    summary: (n) => [String(n.params.prefix || ""), Caps.threads() + "코어"]
  },
  result: {
    title: "결과", out: null, ins: [{ port: "runs", label: "솔버", multi: true }], group: "analysis",
    fields: [], defaults: {},
    summary: () => ["실행 목록에서 확인"]
  }
};

const STAGE_LABEL = {
  geometry: "형상 준비 중", meshing: "메쉬 생성 중", deck: "덱 작성 중",
  solver: "해석 중", post: "후처리 중", report: "리포트 작성 중"
};

const PROVENANCE_LABEL = {
  geometry: "형상에서 읽음",
  template: "추천값 · 근거 보기",
  user: "직접 입력",
  unknown: "확인 필요"
};

/** 목표를 한 줄로. 상·하한이 없는 목표는 "없음"이다(빈 범위를 숫자처럼 쓰지 않는다). */
function targetSentence(target) {
  if (!target) return "없음";
  const lower = target.lower === null || target.lower === undefined ? null : target.lower;
  const upper = target.upper === null || target.upper === undefined ? null : target.upper;
  if (lower === null && upper === null) return "없음 (지표 " + target.metric_key + ", 범위 미입력)";
  const span = lower !== null && upper !== null
    ? num(lower) + "–" + num(upper)
    : (lower !== null ? num(lower) + " 이상" : num(upper) + " 이하");
  return target.metric_key + " " + span + " " + (target.unit || "");
}

function directionSentence(direction, stroke) {
  const d = Array.isArray(direction) ? direction : [0, 0, -1];
  const axis = Math.abs(d[2]) >= Math.abs(d[1]) && Math.abs(d[2]) >= Math.abs(d[0]) ? 2 : (Math.abs(d[1]) >= Math.abs(d[0]) ? 1 : 0);
  const sign = d[axis] < 0 ? -1 : 1;
  const words = [["+X 방향으로", "-X 방향으로"], ["옆에서 안쪽으로", "옆에서 안쪽으로"], ["위에서 아래로", "아래에서 위로"]];
  const label = axis === 2 ? (sign < 0 ? words[2][0] : words[2][1]) : words[axis][sign < 0 ? 1 : 0];
  return label + (stroke ? " " + num(stroke) + " mm 누름" : " 누름");
}

/* ---------------------------------------------------------- GraphModel */

/** 명령 하나. ``redo``/``undo``는 모델을 인자로 받아 반대 방향으로 움직인다. */
function command(label, redo, undo) {
  return { label, redo, undo };
}

class GraphModel {
  constructor() {
    this.reset();
    this.listeners = [];
  }

  reset() {
    this.nodes = [];
    this.edges = [];
    this.meta = {
      schema_version: 2, revision: 0, purpose: null, mesh_preset: null,
      asset_refs: {}, targets: [], provenance: {}, confirmations: {}
    };
    this.extras = {};        // 알 수 없는 최상위 키는 보존한다(U14)
    this.readOnly = false;
    this.unknownNodes = [];  // 알 수 없는 노드 타입도 잃지 않는다(U14)
    this.name = null;
    this._undo = [];
    this._redo = [];
    this._nextId = 1;
  }

  on(fn) { this.listeners.push(fn); }
  emit(reason) { for (const fn of this.listeners) fn(reason, this); }

  /* -- 조회 ------------------------------------------------------------ */

  node(id) { return this.nodes.find((n) => n.id === id) || null; }
  byType(type) { return this.nodes.filter((n) => n.type === type); }
  sources(nodeId, port) {
    return this.edges.filter((e) => e.to === nodeId && e.port === port)
      .map((e) => this.node(e.from)).filter(Boolean);
  }
  targetsOf(nodeId) { return this.edges.filter((e) => e.from === nodeId); }
  solver() { return this.byType("solver")[0] || null; }

  /** 형상 체인의 시작(캔). 컴파일러와 같은 규칙으로 거슬러 올라간다. */
  geometryNode() {
    const meshes = this.byType("mesh");
    for (const mesh of meshes) {
      let cur = this.sources(mesh.id, "geom")[0];
      let guard = 0;
      while (cur && guard++ < 16) {
        if (cur.type === "geometry") return cur;
        cur = this.sources(cur.id, "geom")[0];
      }
    }
    return this.byType("geometry")[0] || null;
  }

  chainNodes() {
    const out = [];
    const geo = this.geometryNode();
    if (!geo) return out;
    let cur = geo;
    let guard = 0;
    while (cur && guard++ < 16) {
      out.push(cur);
      cur = this.edges.filter((e) => e.from === cur.id && e.port === "geom")
        .map((e) => this.node(e.to)).find((n) => n && (n.type === "cap" || n.type === "vent"));
    }
    return out;
  }

  /* -- 명령 ------------------------------------------------------------ */

  apply(cmd) {
    cmd.redo(this);
    this._undo.push(cmd);
    this._redo.length = 0;
    this.emit("change");
    return cmd;
  }
  undo() {
    const cmd = this._undo.pop();
    if (!cmd) return null;
    cmd.undo(this);
    this._redo.push(cmd);
    this.emit("change");
    announce("되돌렸습니다: " + cmd.label);
    return cmd;
  }
  redo() {
    const cmd = this._redo.pop();
    if (!cmd) return null;
    cmd.redo(this);
    this._undo.push(cmd);
    this.emit("change");
    return cmd;
  }
  canUndo() { return this._undo.length > 0; }
  canRedo() { return this._redo.length > 0; }

  newId() { return "n" + this._nextId++; }

  addNode(type, x, y, params, id) {
    const def = NODE_TYPES[type];
    const node = {
      id: id || this.newId(), type,
      x: x === undefined ? 40 : x, y: y === undefined ? 40 : y,
      params: Object.assign({}, def ? def.defaults : {}, params || {})
    };
    this.apply(command("노드 추가",
      (m) => { if (!m.node(node.id)) m.nodes.push(node); },
      (m) => { m.nodes = m.nodes.filter((n) => n.id !== node.id); }));
    return node;
  }

  removeNode(id) {
    const node = this.node(id);
    if (!node) return;
    const index = this.nodes.indexOf(node);
    const dropped = this.edges.filter((e) => e.from === id || e.to === id);
    this.apply(command("노드 삭제",
      (m) => {
        m.nodes = m.nodes.filter((n) => n.id !== id);
        m.edges = m.edges.filter((e) => e.from !== id && e.to !== id);
      },
      (m) => {
        m.nodes.splice(Math.min(index, m.nodes.length), 0, node);
        for (const e of dropped) if (!m.edges.some((x) => x.from === e.from && x.to === e.to && x.port === e.port)) m.edges.push(e);
      }));
  }

  connect(fromId, toId, port, replaced) {
    const edge = { from: fromId, to: toId, port };
    const dropped = replaced || [];
    this.apply(command("연결",
      (m) => {
        for (const d of dropped) m.edges = m.edges.filter((e) => !(e.from === d.from && e.to === d.to && e.port === d.port));
        if (!m.edges.some((e) => e.from === fromId && e.to === toId && e.port === port)) m.edges.push(edge);
      },
      (m) => {
        m.edges = m.edges.filter((e) => !(e.from === fromId && e.to === toId && e.port === port));
        for (const d of dropped) m.edges.push(d);
      }));
  }

  disconnect(edge) {
    this.apply(command("연결 삭제",
      (m) => { m.edges = m.edges.filter((e) => !(e.from === edge.from && e.to === edge.to && e.port === edge.port)); },
      (m) => { m.edges.push({ from: edge.from, to: edge.to, port: edge.port }); }));
  }

  setParam(nodeId, key, value, provenance) {
    const node = this.node(nodeId);
    if (!node) return;
    const before = node.params[key];
    const path = nodeId + "." + key;
    const beforeProv = this.meta.provenance[path];
    const source = provenance || "user";
    this.apply(command("값 변경",
      (m) => {
        const n = m.node(nodeId);
        if (!n) return;
        if (value === undefined || value === "") delete n.params[key];
        else n.params[key] = value;
        m.meta.provenance[path] = source;
      },
      (m) => {
        const n = m.node(nodeId);
        if (!n) return;
        if (before === undefined) delete n.params[key];
        else n.params[key] = before;
        if (beforeProv === undefined) delete m.meta.provenance[path];
        else m.meta.provenance[path] = beforeProv;
      }));
  }

  /** 안내 흐름이 여러 값을 한 번에 바꿀 때(목적 전환 등) 하나의 명령으로 묶는다. */
  batch(label, mutate) {
    const before = JSON.stringify({ nodes: this.nodes, edges: this.edges, meta: this.meta });
    const model = this;
    mutate(model);
    const after = JSON.stringify({ nodes: this.nodes, edges: this.edges, meta: this.meta });
    if (before === after) { this.emit("change"); return; }
    const restore = (text) => (m) => {
      const data = JSON.parse(text);
      m.nodes = data.nodes; m.edges = data.edges; m.meta = data.meta;
    };
    // batch 안에서 이미 적용했으므로 redo는 "after 상태로" 되돌리는 형태다.
    this._undo.push(command(label, restore(after), restore(before)));
    this._redo.length = 0;
    this.emit("change");
  }

  moveNode(id, x, y) {
    const node = this.node(id);
    if (!node) return;
    node.x = x; node.y = y;
    this.emit("move");
  }

  provenance(nodeId, key) { return this.meta.provenance[nodeId + "." + key] || null; }

  /* -- 직렬화 ---------------------------------------------------------- */

  load(json, name) {
    this.reset();
    this.name = name || null;
    const known = ["nodes", "edges", "schema_version", "revision", "purpose", "mesh_preset",
      "asset_refs", "targets", "provenance", "confirmations", "base_revision"];
    for (const key in json) if (!known.includes(key)) this.extras[key] = json[key];
    this.meta.schema_version = json.schema_version || 1;
    this.meta.revision = json.revision || 0;
    this.meta.purpose = json.purpose || null;
    this.meta.mesh_preset = json.mesh_preset || null;
    this.meta.asset_refs = json.asset_refs || {};
    this.meta.targets = json.targets || [];
    this.meta.provenance = json.provenance || {};
    this.meta.confirmations = json.confirmations || {};
    for (const raw of json.nodes || []) {
      const node = { id: raw.id, type: raw.type, x: raw.x || 40, y: raw.y || 40, params: raw.params || {} };
      if (!NODE_TYPES[raw.type]) {
        // U14: 미지의 노드는 버리지 않는다. 원본을 보존하고 읽기 전용으로 연다.
        this.unknownNodes.push(raw);
        this.readOnly = true;
        node.unknown = true;
      }
      this.nodes.push(node);
      const digits = parseInt(String(raw.id).replace(/\D/g, ""), 10);
      if (!Number.isNaN(digits)) this._nextId = Math.max(this._nextId, digits + 1);
    }
    this.edges = (json.edges || []).filter((e) => this.node(e.from) && this.node(e.to));
    this._undo.length = 0;
    this._redo.length = 0;
    this.emit("load");
  }

  toJSON() {
    return Object.assign({}, this.extras, {
      schema_version: 2,
      purpose: this.meta.purpose,
      mesh_preset: this.meta.mesh_preset,
      asset_refs: this.meta.asset_refs,
      targets: this.meta.targets,
      provenance: this.meta.provenance,
      confirmations: this.meta.confirmations,
      nodes: this.nodes.map((n) => ({ id: n.id, type: n.type, x: Math.round(n.x), y: Math.round(n.y), params: n.params })),
      edges: this.edges.map((e) => ({ from: e.from, to: e.to, port: e.port }))
    });
  }
}

/* ------------------------------------------------------------ AppState */

const App = {
  graph: new GraphModel(),
  step: 1,
  selectedNode: null,
  asset: null,            // 가져온 STEP 자산 레코드
  preflight: null,
  preflightState: "idle", // idle | running | ready | error
  preflightError: null,
  lastGraphHash: null,
  submitSeq: 0,
  showSettingsGroup: true,
  compare: [],            // A/B로 고른 실행 id
  runs: [],
  results: new Map(),
  tracked: new Set(),
  lastPollOk: null,
  pollFailed: false,
  saveTimer: null,
  previewLod: null
};

/* ------------------------------------------------------------- 3D 미리보기 */

const Preview = {
  renderer: null,
  init() {
    const canvas = $("preview3d");
    this.renderer = Render3D.create(canvas, { clearColor: "#eef2f6" });
    if (!this.renderer) {
      Render3D.showUnavailable(canvas, {
        html: "<div><b>3D 미리보기를 표시할 수 없습니다</b><br>이 브라우저에서는 WebGL을 쓸 수 없습니다."
          + "<br>치수와 조건 입력·해석 실행은 그대로 사용할 수 있습니다.</div>"
      });
      return;
    }
    this.renderer.attachControls();
    addEventListener("resize", () => this.renderer && this.renderer.requestDraw());
  },
  note(text) {
    const node = $("previewNote");
    node.hidden = !text;
    node.textContent = text || "";
  },
  /** 서버가 만든 표시용 메쉬(자산 미리보기). 원본 정확도와 구별해 표시한다. */
  showAsset(preview) {
    if (!this.renderer) return;
    const info = this.renderer.setMesh({
      positions: Float32Array.from(preview.positions),
      indices: Uint32Array.from(preview.indices),
      color: [0.62, 0.66, 0.72],
      edges: false
    });
    this.renderer.setView("iso");
    this.renderer.requestDraw();
    App.previewLod = preview.lod;
    this.note("표시용 경량 메쉬 (" + String(preview.lod || "preview") + ", 삼각형 " + info.triangles + "개) — 해석 메쉬가 아닙니다");
  },
  /** 파라메트릭 치수의 표시용 형상. 계산이 아니라 그림이다. */
  showParametric(params) {
    if (!this.renderer) return;
    const positions = [];
    const indices = [];
    const h = Number(params.height) || 100;
    if (params.kind === "box_can") {
      const w = (Number(params.width) || 100) / 2;
      const d = (Number(params.depth) || 20) / 2;
      const corners = [[-w, -d, 0], [w, -d, 0], [w, d, 0], [-w, d, 0], [-w, -d, h], [w, -d, h], [w, d, h], [-w, d, h]];
      for (const c of corners) positions.push(c[0], c[1], c[2]);
      const faces = [[0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6], [0, 4, 5], [0, 5, 1],
        [1, 5, 6], [1, 6, 2], [2, 6, 7], [2, 7, 3], [3, 7, 4], [3, 4, 0]];
      for (const f of faces) indices.push(f[0], f[1], f[2]);
    } else {
      const r = Number(params.radius) || 30;
      const n = 40;
      for (let i = 0; i < n; i++) {
        const a = (i / n) * Math.PI * 2;
        positions.push(r * Math.cos(a), r * Math.sin(a), 0);
        positions.push(r * Math.cos(a), r * Math.sin(a), h);
      }
      for (let i = 0; i < n; i++) {
        const j = (i + 1) % n;
        indices.push(i * 2, j * 2, j * 2 + 1, i * 2, j * 2 + 1, i * 2 + 1);
      }
    }
    this.renderer.setMesh({
      positions: Float32Array.from(positions),
      indices: Uint32Array.from(indices),
      color: [0.66, 0.72, 0.8]
    });
    this.renderer.setView("iso");
    this.renderer.setWireframe(true);
    this.renderer.requestDraw();
    this.note("입력한 치수의 표시용 형상 — 해석 메쉬는 실행할 때 서버가 만듭니다");
  },
  fit() {
    if (!this.renderer) return;
    this.renderer.setView("iso");
    this.renderer.requestDraw();
  }
};

/* --------------------------------------------------------------- 대화상자 */

const Modal = {
  _returnFocus: null,
  open(title, body, actions) {
    const dialog = $("modal");
    this._returnFocus = document.activeElement;
    clear(dialog);
    dialog.appendChild(el("h3", { text: title }));
    dialog.appendChild(body);
    const row = el("div", { class: "modal-actions" });
    for (const action of actions || []) {
      row.appendChild(el("button", {
        class: "btn" + (action.primary ? " primary" : ""),
        text: action.label,
        onclick: () => {
          const keep = action.run ? action.run() : false;
          if (!keep) this.close();
        }
      }));
    }
    dialog.appendChild(row);
    dialog.showModal();
    const focusable = dialog.querySelector("input, select, button.primary, button");
    if (focusable) focusable.focus();
    return dialog;
  },
  close() {
    const dialog = $("modal");
    if (dialog.open) dialog.close();
    if (this._returnFocus && this._returnFocus.focus) this._returnFocus.focus();
  }
};

/* ---------------------------------------------------------- GuidedSetup */

const Guided = {
  /** 치수로 시작: 물리 구성 + 접힌 해석 설정을 가진 최소 그래프. */
  startParametric(kind) {
    const g = App.graph;
    g.reset();
    g.name = null;
    g.batch("치수로 시작", (m) => {
      const geo = { id: m.newId(), type: "geometry", x: 20, y: 40, params: Object.assign({}, NODE_TYPES.geometry.defaults) };
      if (kind === "box_can") geo.params = { kind: "box_can", width: 120.5, depth: 13.1, height: 65.0, thickness: 0.6, closed_bottom: true };
      const mesh = { id: m.newId(), type: "mesh", x: 300, y: 40, params: Object.assign({}, NODE_TYPES.mesh.defaults) };
      const mat = { id: m.newId(), type: "material", x: 20, y: 250, params: Object.assign({}, NODE_TYPES.material.defaults) };
      const load = { id: m.newId(), type: "loading", x: 300, y: 250, params: Object.assign({}, NODE_TYPES.loading.defaults) };
      const contact = { id: m.newId(), type: "contact", x: 20, y: 420, params: Object.assign({}, NODE_TYPES.contact.defaults) };
      const solver = { id: m.newId(), type: "solver", x: 590, y: 40, params: Object.assign({}, NODE_TYPES.solver.defaults, { prefix: "study_1" }) };
      const result = { id: m.newId(), type: "result", x: 590, y: 300, params: {} };
      m.nodes.push(geo, mesh, mat, load, contact, solver, result);
      m.edges.push({ from: geo.id, to: mesh.id, port: "geom" });
      m.edges.push({ from: mesh.id, to: solver.id, port: "mesh" });
      m.edges.push({ from: mat.id, to: solver.id, port: "mat" });
      m.edges.push({ from: load.id, to: solver.id, port: "load" });
      m.edges.push({ from: contact.id, to: solver.id, port: "contact" });
      m.edges.push({ from: solver.id, to: result.id, port: "runs" });
      for (const key in geo.params) m.meta.provenance[geo.id + "." + key] = "template";
      for (const key in load.params) m.meta.provenance[load.id + "." + key] = "template";
      m.meta.provenance[mat.id + ".key"] = "template";
    });
    App.step = 1;
    App.selectedNode = null;
    UI.hideStart();
    UI.renderAll();
  },

  /** 목적 선택: capability의 템플릿에 맞게 그래프를 구성한다(§4 2단계). */
  applyPurpose(purposeId) {
    const purpose = Caps.purpose(purposeId);
    if (!purpose || !purpose.available) return;
    const g = App.graph;
    g.batch("해석 목적 적용", (m) => {
      m.meta.purpose = purposeId;
      const solver = m.solver();
      if (solver && purpose.load_case) solver.params.load_case = purpose.load_case;
      // 실행 이름은 ASCII만 남는다(graphc가 그 외 문자를 _로 바꾼다). 사용자가
      // 아직 손대지 않은 기본 이름이면 목적에 맞는 이름을 제안한다.
      if (solver && ["graph_case", "study_1", "", undefined].includes(solver.params.prefix)) {
        solver.params.prefix = purposeId + "_1";
        m.meta.provenance[solver.id + ".prefix"] = "template";
      }
      const preset = Caps.defaultPreset(purposeId);
      m.meta.mesh_preset = preset ? preset.id : null;
      const geo = m.geometryNode();
      const mesh = m.byType("mesh")[0];
      if (purposeId === "vent_burst") {
        if (geo && geo.params.kind === "parametric_can") {
          geo.params = { kind: "box_can", width: 120.5, depth: 13.1, height: 65.0, thickness: 0.6, closed_bottom: true };
          for (const key in geo.params) m.meta.provenance[geo.id + "." + key] = "template";
        }
        // 캔 → 캡 → 벤트 체인을 만든다(벤트는 캡 뒤에만 붙는다).
        let cap = m.byType("cap")[0];
        let vent = m.byType("vent")[0];
        if (geo && !cap) {
          cap = { id: m.newId(), type: "cap", x: geo.x + 260, y: geo.y, params: Object.assign({}, NODE_TYPES.cap.defaults) };
          m.nodes.push(cap);
        }
        if (!vent) {
          vent = { id: m.newId(), type: "vent", x: (cap ? cap.x : 300) + 250, y: (cap ? cap.y : 40), params: Object.assign({}, NODE_TYPES.vent.defaults) };
          m.nodes.push(vent);
          for (const key in vent.params) m.meta.provenance[vent.id + "." + key] = "template";
        }
        if (geo && cap && vent) {
          m.edges = m.edges.filter((e) => !(e.port === "geom" && (e.from === geo.id || e.from === cap.id || e.from === vent.id)));
          m.edges.push({ from: geo.id, to: cap.id, port: "geom" });
          m.edges.push({ from: cap.id, to: vent.id, port: "geom" });
          if (mesh) m.edges.push({ from: vent.id, to: mesh.id, port: "geom" });
          if (mesh) { mesh.x = Math.max(mesh.x, vent.x + 250); mesh.y = vent.y; }
        }
        // 하중은 내압으로 바꾼다(공구 하중 노드는 지운다).
        const solverNode = m.solver();
        let pressure = m.byType("pressure")[0];
        if (!pressure) {
          pressure = { id: m.newId(), type: "pressure", x: 300, y: 430, params: Object.assign({}, NODE_TYPES.pressure.defaults, { peak: 0.85, rise: 0.003, hold: 0.0005, brace_walls: true }) };
          m.nodes.push(pressure);
          for (const key in pressure.params) m.meta.provenance[pressure.id + "." + key] = "template";
        }
        for (const tool of m.byType("loading")) {
          m.edges = m.edges.filter((e) => e.from !== tool.id && e.to !== tool.id);
          m.nodes = m.nodes.filter((n) => n.id !== tool.id);
        }
        if (solverNode) {
          m.edges = m.edges.filter((e) => !(e.to === solverNode.id && e.port === "load"));
          m.edges.push({ from: pressure.id, to: solverNode.id, port: "load" });
        }
        if (!m.meta.targets.length) {
          m.meta.targets = [{ metric_key: "vent_opening_pressure", lower: null, upper: null, unit: "MPa", inclusive: true }];
        }
      } else {
        // 압착·축방향: 내압 노드를 공구 하중으로 되돌린다.
        const solverNode = m.solver();
        let tool = m.byType("loading")[0];
        if (!tool) {
          tool = { id: m.newId(), type: "loading", x: 300, y: 250, params: Object.assign({}, NODE_TYPES.loading.defaults) };
          m.nodes.push(tool);
          for (const key in tool.params) m.meta.provenance[tool.id + "." + key] = "template";
        }
        tool.params.direction = purposeId === "axial_crush" ? [0, 0, -1] : [0, 1, 0];
        for (const p of m.byType("pressure")) {
          m.edges = m.edges.filter((e) => e.from !== p.id && e.to !== p.id);
          m.nodes = m.nodes.filter((n) => n.id !== p.id);
        }
        if (solverNode) {
          m.edges = m.edges.filter((e) => !(e.to === solverNode.id && e.port === "load"));
          m.edges.push({ from: tool.id, to: solverNode.id, port: "load" });
        }
        if (m.meta.targets.length && m.meta.targets[0].metric_key === "vent_opening_pressure") m.meta.targets = [];
      }
      // §4.1: 목적이 바뀌면 영향받는 확인만 해제한다.
      delete m.meta.confirmations.load_direction;
      delete m.meta.confirmations.support;
    });
    App.step = 3;
    UI.renderAll();
  },

  /** 자산을 형상 노드에 붙인다. step_path는 고급 가져오기에만 남긴다. */
  attachAsset(record) {
    const g = App.graph;
    App.asset = record;
    g.batch("STEP 형상 연결", (m) => {
      let geo = m.geometryNode();
      if (!geo) {
        geo = { id: m.newId(), type: "geometry", x: 20, y: 40, params: {} };
        m.nodes.push(geo);
      }
      geo.params.kind = "step";
      delete geo.params.step_path;   // 서버 경로는 UI가 만들지 않는다
      m.meta.asset_refs = {};
      m.meta.asset_refs[geo.id] = record.id;
      const dims = record.dimensions_mm || [];
      if (dims.length === 3) {
        geo.params.width = Number(dims[0].toFixed ? dims[0].toFixed(2) : dims[0]);
        geo.params.depth = Number(dims[1].toFixed ? dims[1].toFixed(2) : dims[1]);
        geo.params.height = Number(dims[2].toFixed ? dims[2].toFixed(2) : dims[2]);
        for (const key of ["width", "depth", "height"]) m.meta.provenance[geo.id + "." + key] = "geometry";
      }
      const gauged = (record.parts || []).map((p) => p.wall_thickness_mm).filter((t) => t);
      if (gauged.length) {
        geo.params.thickness = Number(gauged[0].toFixed ? gauged[0].toFixed(3) : gauged[0]);
        m.meta.provenance[geo.id + ".thickness"] = "geometry";
      } else {
        m.meta.provenance[geo.id + ".thickness"] = "unknown";
      }
      delete m.meta.confirmations.units;
    });
  },

  /** 필수 확인 묶음(§4.1). 실행 전에 사용자가 확인해야 하는 항목. */
  confirmations() {
    const g = App.graph;
    const out = [];
    const solver = g.solver();
    const material = g.byType("material")[0];
    out.push({
      key: "material",
      label: "재료",
      detail: material ? (Caps.material(material.params.key) ? Caps.material(material.params.key).name : String(material.params.key || "")) : "미지정",
      hint: "형상 파일만으로 재료를 확인할 수 없습니다."
    });
    const load = g.byType("pressure")[0] || g.byType("loading")[0];
    if (load) {
      out.push({
        key: "load_direction",
        label: load.type === "pressure" ? "가압 조건" : "하중 방향",
        detail: load.type === "pressure"
          ? "내압 최대 " + num(load.params.peak) + " MPa"
          : directionSentence(load.params.direction, load.params.stroke),
        hint: "이 방향으로 하중이 가해집니다."
      });
      out.push({
        key: "support",
        label: "고정·지지",
        detail: load.params.clamp_can_base ? "바닥 고정" : (load.params.support || "지정 없음"),
        hint: "이 면은 해석 중 움직이지 않습니다."
      });
    }
    if (App.asset && App.asset.units && App.asset.units.plausible === false) {
      out.push({ key: "units", label: "단위", detail: String((App.asset.units || {}).declared || "?"), hint: "읽은 크기가 예상한 셀 크기와 맞나요?" });
    }
    if (solver && !solver.params.prefix) out.push({ key: "name", label: "검토 이름", detail: "미입력", hint: "실행을 알아볼 이름이 필요합니다." });
    return out;
  },

  pendingConfirmations() {
    return this.confirmations().filter((c) => !App.graph.meta.confirmations[c.key]);
  },

  confirm(key, on) {
    const g = App.graph;
    g.batch("확인", (m) => {
      if (on === false) delete m.meta.confirmations[key];
      else m.meta.confirmations[key] = true;
    });
    UI.renderAll();
  },

  /** 조건 하나를 복제해 두 번째 케이스를 만든다(§7.3). */
  compareOne(nodeId, key, newValue) {
    const g = App.graph;
    const node = g.node(nodeId);
    if (!node) return;
    g.batch("조건 하나 바꿔 비교", (m) => {
      const solver = m.solver();
      const original = m.node(nodeId);
      const clone = {
        id: m.newId(), type: original.type, x: original.x, y: original.y + 200,
        params: Object.assign({}, original.params)
      };
      clone.params[key] = newValue;
      if (!original.params.tag) original.params.tag = "a";
      clone.params.tag = "b";
      m.nodes.push(clone);
      m.meta.provenance[clone.id + "." + key] = "user";
      if (["mesh", "material", "loading", "pressure"].includes(original.type)) {
        const port = original.type === "mesh" ? "mesh" : (original.type === "material" ? "mat" : "load");
        // 메쉬 노드를 복제하면 형상 입력도 같이 이어야 한다.
        if (original.type === "mesh") {
          for (const src of m.sources(original.id, "geom")) m.edges.push({ from: src.id, to: clone.id, port: "geom" });
        }
        if (solver) m.edges.push({ from: clone.id, to: solver.id, port: port });
      } else {
        // 형상 체인(캔·캡·벤트)의 값이면 그 아래 메쉬까지 복제해 두 갈래로 만든다.
        const downstream = m.edges.filter((e) => e.from === original.id && e.port === "geom").map((e) => m.node(e.to)).filter(Boolean);
        for (const target of downstream) {
          if (target.type === "mesh") {
            const meshClone = {
              id: m.newId(), type: "mesh", x: target.x, y: target.y + 200,
              params: Object.assign({}, target.params, { tag: "b" })
            };
            if (!target.params.tag) target.params.tag = "a";
            m.nodes.push(meshClone);
            m.edges.push({ from: clone.id, to: meshClone.id, port: "geom" });
            if (solver) m.edges.push({ from: meshClone.id, to: solver.id, port: "mesh" });
          }
        }
        for (const src of m.sources(original.id, "geom")) m.edges.push({ from: src.id, to: clone.id, port: "geom" });
      }
    });
    App.step = 4;
    UI.renderAll();
  }
};

/* ----------------------------------------------------------- GraphView */

const GraphView = {
  armed: null,       // 클릭해 둔 출력 포트
  message(text, kind) {
    const node = $("graphMsg");
    node.textContent = text || "";
    node.style.color = kind === "info" ? "var(--ui-muted)" : "var(--ui-danger)";
  },

  hidden(node) {
    return App.showSettingsGroup && NODE_TYPES[node.type] && NODE_TYPES[node.type].group === "analysis"
      && node.type !== "result";
  },

  render() {
    const canvas = $("graphCanvas");
    const wires = $("graphWires");
    for (const old of [...canvas.querySelectorAll(".gnode")]) old.remove();
    const g = App.graph;
    for (const node of g.nodes) {
      if (this.hidden(node)) continue;
      canvas.appendChild(this.nodeElement(node));
    }
    if (App.showSettingsGroup && g.nodes.some((n) => this.hidden(n))) canvas.appendChild(this.groupElement());
    this.drawEdges();
    void wires;
  },

  groupElement() {
    const g = App.graph;
    const members = g.nodes.filter((n) => this.hidden(n));
    const x = Math.max.apply(null, members.map((n) => n.x).concat([560]));
    const y = Math.min.apply(null, members.map((n) => n.y).concat([40]));
    const solver = g.solver();
    const blocked = (App.preflight && App.preflight.checks || []).filter((c) => c.severity === "block");
    const box = el("div", { class: "gnode", id: "gnode-settings", style: `left:${x}px;top:${y}px` }, [
      el("h4", {}, ["해석 설정 그룹"]),
      el("div", { class: "gbody" }, [
        el("div", { class: "row" }, [el("span", { text: Caps.threads() + "코어" }),
          el("b", { text: solver ? String(solver.params.prefix || "") : "" })]),
        el("div", { class: "row" }, [el("span", { text: "메쉬·접촉·솔버" }),
          el("b", { text: String(members.length) + "개 노드" })]),
        blocked.length
          ? el("div", { class: "badge bad", text: "실행 차단 " + blocked.length + "건" })
          : el("div", { class: "small muted", text: "펼치면 개별 노드를 볼 수 있습니다" })
      ])
    ]);
    box.addEventListener("click", () => { App.showSettingsGroup = false; UI.renderAll(); });
    return box;
  },

  nodeElement(node) {
    const def = NODE_TYPES[node.type];
    const box = el("div", {
      class: "gnode" + (App.selectedNode === node.id ? " selected" : "") + (node.unknown ? " readonly" : ""),
      id: "gnode-" + node.id,
      style: `left:${node.x}px;top:${node.y}px`,
      tabindex: "0",
      role: "group",
      "aria-label": (def ? def.title : node.type) + " 노드"
    });
    const title = el("h4", { text: def ? def.title : node.type + " (알 수 없는 노드)" });
    box.appendChild(title);
    if (!node.unknown) {
      box.appendChild(el("button", {
        class: "del", "aria-label": (def ? def.title : node.type) + " 노드 삭제", text: "✕",
        onclick: (e) => { e.stopPropagation(); this.removeNodeWithUndo(node.id); }
      }));
    }
    const body = el("div", { class: "gbody" });
    const rows = def && def.summary ? def.summary(node) : ["원본을 보존했습니다 (읽기 전용)"];
    for (const row of rows) body.appendChild(el("div", { class: "row" }, [el("span", { text: row })]));
    box.appendChild(body);

    const ins = (def && def.ins) || [];
    ins.forEach((pin, index) => {
      const port = el("button", {
        class: "port in", style: `top:${34 + index * 20}px`, type: "button",
        "aria-label": pin.label + " 입력 포트",
        dataset: { node: node.id, port: pin.port, dir: "in" }
      }, [el("span", { class: "plabel", text: pin.label })]);
      port.addEventListener("click", (e) => { e.stopPropagation(); this.clickPort(port); });
      box.appendChild(port);
    });
    if (def && def.out) {
      const port = el("button", {
        class: "port out", style: "top:12px", type: "button",
        "aria-label": def.out + " 출력 포트",
        dataset: { node: node.id, type: def.out, dir: "out" }
      });
      port.addEventListener("click", (e) => { e.stopPropagation(); this.clickPort(port); });
      box.appendChild(port);
    }

    box.addEventListener("click", () => { App.selectedNode = node.id; UI.renderAll(); });
    title.addEventListener("mousedown", (event) => {
      event.preventDefault();
      const startX = event.clientX - box.offsetLeft;
      const startY = event.clientY - box.offsetTop;
      const move = (ev) => {
        const x = Math.max(0, ev.clientX - startX);
        const y = Math.max(0, ev.clientY - startY);
        box.style.left = x + "px";
        box.style.top = y + "px";
        App.graph.moveNode(node.id, x, y);
        this.drawEdges();
      };
      const up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); UI.scheduleSave(); };
      addEventListener("mousemove", move);
      addEventListener("mouseup", up);
    });
    return box;
  },

  removeNodeWithUndo(id) {
    const node = App.graph.node(id);
    App.graph.removeNode(id);
    const def = NODE_TYPES[node ? node.type : ""];
    this.message((def ? def.title : "노드") + "을(를) 삭제했습니다. Ctrl+Z로 되돌릴 수 있습니다.", "info");
    UI.renderAll();
  },

  /** 포트 클릭: 출력 → 호환 입력 강조 → 입력 클릭으로 연결(§7.2). */
  clickPort(portEl) {
    const g = App.graph;
    if (portEl.dataset.dir === "out") {
      this.setArmed(portEl);
      return;
    }
    if (!this.armed) {
      this.message("먼저 출력 포트(주황)를 클릭하세요.", "info");
      return;
    }
    const fromId = this.armed.dataset.node;
    const fromType = this.armed.dataset.type;
    const toId = portEl.dataset.node;
    const toPort = portEl.dataset.port;
    this.setArmed(null);
    const reason = this.connectionError(fromId, fromType, toId, toPort);
    if (reason) {
      // 거부해도 기존 연결은 그대로 둔다(U12).
      this.message(reason);
      announce(reason);
      return;
    }
    const def = NODE_TYPES[g.node(toId).type];
    const pin = def.ins.find((i) => i.port === toPort);
    const existing = g.edges.filter((e) => e.to === toId && e.port === toPort);
    if (!pin.multi && existing.length) {
      const from = g.node(existing[0].from);
      Modal.open("기존 연결을 교체할까요?",
        el("p", { text: "'" + (NODE_TYPES[from.type] ? NODE_TYPES[from.type].title : from.type) + "' 연결이 이미 있습니다. 새 연결로 교체하면 이전 연결은 사라집니다." }),
        [
          { label: "취소" },
          {
            label: "교체", primary: true,
            run: () => { g.connect(fromId, toId, toPort, existing.slice()); this.message("연결을 교체했습니다. Ctrl+Z로 되돌릴 수 있습니다.", "info"); UI.renderAll(); }
          }
        ]);
      return;
    }
    g.connect(fromId, toId, toPort, []);
    this.message("");
    UI.renderAll();
  },

  connectionError(fromId, fromType, toId, toPort) {
    const g = App.graph;
    if (fromId === toId) return "자기 자신에는 연결할 수 없습니다.";
    if (fromType !== toPort) return "포트 종류가 다릅니다: " + fromType + " → " + toPort + " 연결은 만들 수 없습니다.";
    if (g.edges.some((e) => e.from === fromId && e.to === toId && e.port === toPort)) return "이미 연결되어 있습니다.";
    // 순환 금지: to에서 출발해 from에 닿으면 고리가 된다.
    const seen = new Set();
    const walk = (id) => {
      if (id === fromId) return true;
      if (seen.has(id)) return false;
      seen.add(id);
      return g.edges.filter((e) => e.from === id).some((e) => walk(e.to));
    };
    if (walk(toId)) return "연결이 고리를 만듭니다. 다른 노드를 선택하세요.";
    return null;
  },

  setArmed(portEl) {
    if (this.armed) this.armed.classList.remove("armed");
    for (const port of [...document.querySelectorAll(".port.in")]) port.classList.remove("compat", "incompat");
    this.armed = portEl || null;
    if (!portEl) return;
    portEl.classList.add("armed");
    const type = portEl.dataset.type;
    for (const port of [...document.querySelectorAll(".port.in")]) {
      port.classList.add(port.dataset.port === type ? "compat" : "incompat");
    }
    this.message("연결할 입력 포트를 클릭하세요 (호환 포트만 강조됩니다).", "info");
  },

  portPos(nodeId, port, dir) {
    const g = App.graph;
    const node = g.node(nodeId);
    let box = $("gnode-" + nodeId);
    if (!box && node && this.hidden(node)) box = $("gnode-settings");
    if (!box) return null;
    const selector = dir === "out" ? ".port.out" : '.port.in[data-port="' + port + '"]';
    const target = box.querySelector(selector);
    const canvas = $("graphCanvas").getBoundingClientRect();
    const rect = (target || box).getBoundingClientRect();
    return [rect.left - canvas.left + rect.width / 2, rect.top - canvas.top + rect.height / 2];
  },

  drawEdges() {
    const canvas = $("graphCanvas");
    let width = 1200;
    let height = 760;
    for (const node of App.graph.nodes) {
      if (this.hidden(node)) continue;
      const box = $("gnode-" + node.id);
      width = Math.max(width, node.x + (box ? box.offsetWidth : 220) + 80);
      height = Math.max(height, node.y + (box ? box.offsetHeight : 160) + 80);
    }
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    const svg = $("graphWires");
    clear(svg);
    App.graph.edges.forEach((edge) => {
      const a = this.portPos(edge.from, null, "out");
      const b = this.portPos(edge.to, edge.port, "in");
      if (!a || !b) return;
      const mid = (a[0] + b[0]) / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${a[0]},${a[1]} C${mid},${a[1]} ${mid},${b[1]} ${b[0]},${b[1]}`);
      path.addEventListener("click", () => {
        App.graph.disconnect(edge);
        this.message("연결을 삭제했습니다. Ctrl+Z로 되돌릴 수 있습니다.", "info");
        UI.renderAll();
      });
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = "클릭하면 연결 삭제";
      path.appendChild(title);
      svg.appendChild(path);
    });
  }
};

/* ----------------------------------------------------------- Inspector */

const Inspector = {
  render() {
    const panel = clear($("inspector"));
    if (App.graph.readOnly) {
      panel.appendChild(el("div", { class: "note bad" }, [
        "이 그래프에는 이 버전이 모르는 노드가 있어 읽기 전용으로 열었습니다. 원본은 그대로 보존됩니다."
      ]));
    }
    if (App.selectedNode) { this.renderNode(panel); return; }
    if (App.step === 1) this.stepGeometry(panel);
    else if (App.step === 2) this.stepPurpose(panel);
    else if (App.step === 3) this.stepConditions(panel);
    else this.stepReview(panel);
  },

  /* -- 1단계: 형상 --------------------------------------------------- */

  stepGeometry(panel) {
    panel.appendChild(el("h2", { class: "section-title", text: "1. 형상" }));
    const geo = App.graph.geometryNode();
    if (!geo) {
      panel.appendChild(el("p", { class: "muted", text: "시작 영역에서 형상을 먼저 정하세요." }));
      panel.appendChild(el("button", { class: "btn", text: "시작 화면 열기", onclick: () => UI.showStart() }));
      return;
    }
    if (geo.params.kind === "step") {
      panel.appendChild(this.assetCard());
    } else {
      panel.appendChild(el("div", { class: "field" }, [
        el("label", { for: "geoKind", text: "캔 종류" }),
        el("div", { class: "control" }, [
          el("select", {
            id: "geoKind",
            onchange: (e) => {
              App.graph.setParam(geo.id, "kind", e.target.value);
              if (e.target.value === "box_can" && !geo.params.width) {
                App.graph.setParam(geo.id, "width", 120.5);
                App.graph.setParam(geo.id, "depth", 13.1);
              }
              UI.renderAll();
            }
          }, [
            el("option", { value: "parametric_can", text: "원통형 캔", selected: geo.params.kind === "parametric_can" }),
            el("option", { value: "box_can", text: "각형 캔", selected: geo.params.kind === "box_can" })
          ])
        ])
      ]));
    }
    for (const field of NODE_TYPES.geometry.fields) {
      if (field.p === "kind" || field.advanced) continue;
      if (field.show && !field.show(geo)) continue;
      panel.appendChild(this.fieldRow(geo, field));
    }
    panel.appendChild(el("div", { class: "note" }, ["형상을 바꾸면 미리보기와 그래프의 형상 노드가 함께 바뀝니다."]));
    panel.appendChild(this.nextButton("알고 싶은 것 고르기", 2));
  },

  assetCard() {
    const record = App.asset;
    const wrap = el("div", { class: "group" });
    if (!record) {
      wrap.appendChild(el("div", { class: "group-body" }, [el("p", { class: "muted", text: "가져온 STEP 자산 정보가 없습니다." })]));
      return wrap;
    }
    const head = el("summary", {}, ["형상 확인 카드"]);
    const details = el("details", { class: "group", open: true }, [head]);
    const body = el("div", { class: "group-body" });
    const table = el("table", { class: "kv" });
    const rows = [
      ["파일", record.display_name || ""],
      ["상태", record.status === "ready" ? "준비됨" : (record.status === "failed" ? "가져오기 실패" : "형상을 읽고 있습니다")],
      ["단위", ((record.units || {}).declared || "?") + ((record.units || {}).plausible === false ? " · 확인 필요" : "")],
      ["외형 치수", (record.dimensions_mm || []).map((v) => num(v)).join(" × ") + " mm"],
      ["솔리드", String((record.parts || []).length) + "개"]
    ];
    for (const [key, value] of rows) {
      table.appendChild(el("tr", {}, [el("th", { text: key }), el("td", { text: String(value) })]));
    }
    body.appendChild(table);
    if ((record.units || {}).plausible === false) {
      body.appendChild(el("div", { class: "note warn" }, ["읽은 크기가 예상한 셀 크기와 맞나요?"]));
      body.appendChild(el("button", {
        class: "btn small", text: "단위를 확인했습니다",
        onclick: () => Guided.confirm("units", true)
      }));
    }
    if ((record.parts || []).length) {
      const list = el("table", { class: "kv" });
      list.appendChild(el("tr", {}, [el("th", { text: "부품" }), el("th", { text: "두께 · 역할 제안" })]));
      for (const part of record.parts) {
        list.appendChild(el("tr", {}, [
          el("td", { text: "#" + part.index + " " + (part.kind || "") }),
          el("td", { text: (part.wall_thickness_mm ? num(part.wall_thickness_mm) + " mm" : "두께 확인 불가") + " · " + (part.role_suggestion || "역할 미정") })
        ]));
      }
      body.appendChild(list);
      body.appendChild(el("p", { class: "small muted", text: "솔리드 수를 부품 역할로 확정하지 않습니다. 역할은 직접 지정할 수 있습니다." }));
    }
    for (const diag of record.diagnostics || []) {
      body.appendChild(el("div", { class: "note " + (diag.severity === "block" ? "bad" : "") , text: String(diag.message || diag.code) }));
    }
    details.appendChild(body);
    return details;
  },

  /* -- 2단계: 목적 --------------------------------------------------- */

  stepPurpose(panel) {
    panel.appendChild(el("h2", { class: "section-title", text: "2. 알고 싶은 것" }));
    panel.appendChild(el("p", { class: "muted", text: "이번 해석에서 무엇을 알고 싶나요?" }));
    const list = el("div", { class: "purpose-list", role: "radiogroup", "aria-label": "해석 목적" });
    const purposes = Caps.purposes();
    purposes.forEach((purpose, index) => {
      const chosen = App.graph.meta.purpose === purpose.id;
      const card = el("button", {
        class: "purpose-card", type: "button", role: "radio",
        "aria-checked": chosen ? "true" : "false",
        "aria-disabled": purpose.available ? "false" : "true",
        tabindex: chosen || (!App.graph.meta.purpose && index === 0) ? "0" : "-1",
        dataset: { purpose: purpose.id }
      }, [
        el("b", { text: purpose.title }),
        el("span", { text: purpose.available ? this.purposeHint(purpose) : String(purpose.unavailable_reason || "준비 중") }),
        purpose.available ? null : el("span", { class: "badge warn", text: "준비 중" }),
        purpose.load_case ? el("span", { class: "small muted", text: purpose.load_case }) : null
      ]);
      card.addEventListener("click", () => {
        if (!purpose.available) { announce(String(purpose.unavailable_reason || "")); return; }
        Guided.applyPurpose(purpose.id);
      });
      card.addEventListener("keydown", (event) => {
        // 방향키로 카드 사이를 이동하고 Enter/Space로 고른다(§12.4).
        const cards = [...list.querySelectorAll(".purpose-card")];
        const at = cards.indexOf(card);
        if (event.key === "ArrowDown" || event.key === "ArrowRight") {
          event.preventDefault();
          cards[(at + 1) % cards.length].focus();
        } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
          event.preventDefault();
          cards[(at - 1 + cards.length) % cards.length].focus();
        } else if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          card.click();
        }
      });
      list.appendChild(card);
    });
    panel.appendChild(list);
    const preset = Caps.defaultPreset(App.graph.meta.purpose);
    if (preset) {
      panel.appendChild(el("div", { class: "note ok" }, [
        el("b", { text: preset.label }), " — ", String(preset.badge || "")
      ]));
    }
    if (App.graph.meta.purpose) panel.appendChild(this.nextButton("조건 확인하기", 3));
  },

  purposeHint(purpose) {
    return {
      lateral_crush: "옆면이나 지정 위치를 눌렀을 때 필요한 힘을 봅니다.",
      axial_crush: "위아래로 눌렀을 때 접힘과 지지 하중을 봅니다.",
      vent_burst: "내압이 올라갈 때 벤트가 언제 얼마나 열리는지 봅니다."
    }[purpose.id] || (purpose.outputs || []).join(" · ");
  },

  /* -- 3단계: 조건 --------------------------------------------------- */

  stepConditions(panel) {
    const g = App.graph;
    panel.appendChild(el("h2", { class: "section-title", text: "3. 조건" }));
    const solver = g.solver();
    if (solver) panel.appendChild(this.nameRow(solver));

    const material = g.byType("material")[0];
    if (material) {
      panel.appendChild(el("h3", { class: "section-title", text: "재료" }));
      panel.appendChild(this.materialSelect(material, "key"));
    }

    const load = g.byType("pressure")[0] || g.byType("loading")[0];
    if (load && load.type === "loading") {
      panel.appendChild(el("h3", { class: "section-title", text: "누르는 방식" }));
      panel.appendChild(this.fieldRow(load, NODE_TYPES.loading.fields.find((f) => f.p === "tool")));
      panel.appendChild(this.directionPicker(load));
      panel.appendChild(this.fieldRow(load, NODE_TYPES.loading.fields.find((f) => f.p === "stroke")));
      panel.appendChild(this.supportPicker(load));
    } else if (load) {
      panel.appendChild(el("h3", { class: "section-title", text: "가압 조건" }));
      panel.appendChild(this.fieldRow(load, NODE_TYPES.pressure.fields.find((f) => f.p === "peak")));
      panel.appendChild(this.supportPicker(load));
    }

    const vent = g.byType("vent")[0];
    if (vent) {
      panel.appendChild(el("h3", { class: "section-title", text: "벤트" }));
      for (const field of NODE_TYPES.vent.fields) {
        if (field.advanced) continue;
        panel.appendChild(field.p === "material" ? this.materialSelect(vent, "material") : this.fieldRow(vent, field));
      }
    }

    panel.appendChild(el("h3", { class: "section-title", text: "설계 목표 (선택)" }));
    panel.appendChild(this.targetEditor());

    panel.appendChild(el("h3", { class: "section-title", text: "필수 확인" }));
    panel.appendChild(this.confirmBlock());
    panel.appendChild(this.nextButton("실행 확인으로", 4));
  },

  /** 검토 이름 + 그 이름이 실제 실행 이름으로 어떻게 바뀌는지(§6.2: 표시명과
      실행 ID를 분리한다). 서버가 영숫자·_·-만 남기므로 미리 보여 준다. */
  nameRow(solver) {
    const wrap = this.fieldRow(solver, { p: "prefix", label: "검토 이름", type: "text" });
    const typed = String(solver.params.prefix || "");
    const runName = typed.replace(/[^A-Za-z0-9_\-]/g, "_").replace(/^[^A-Za-z0-9]+/, "") || "graph_case";
    if (runName !== typed) {
      wrap.appendChild(el("p", { class: "small muted", text: "실행 이름: " + runName + " (영숫자·_·-만 남습니다)" }));
    }
    return wrap;
  },

  materialSelect(node, key) {
    const wrap = el("div", { class: "field" });
    const inputId = "mat-" + node.id + "-" + key;
    wrap.appendChild(el("label", { for: inputId, text: key === "material" ? "포일 재료" : "재료 카드" }));
    const listId = inputId + "-list";
    const control = el("div", { class: "control" });
    const input = el("input", {
      type: "search", id: inputId, list: listId, value: String(node.params[key] || ""),
      placeholder: "재료 이름 또는 카드 키",
      onchange: (e) => {
        const value = e.target.value.trim();
        const card = Caps.materials().find((m) => m.key === value || m.name === value);
        App.graph.setParam(node.id, key, card ? card.key : value);
        delete App.graph.meta.confirmations.material;
        UI.renderAll();
      }
    });
    const list = el("datalist", { id: listId });
    for (const card of Caps.materials()) {
      list.appendChild(el("option", { value: card.key, label: card.name + (card.verified ? " · 검증됨" : " · 검증 미확인") }));
    }
    control.appendChild(input);
    control.appendChild(list);
    wrap.appendChild(control);
    const card = Caps.material(node.params[key]);
    if (card) {
      wrap.appendChild(el("div", { class: "control" }, [
        el("span", { class: "badge " + (card.verified ? "ok" : "warn"), text: card.verified ? "검증됨" : "검증 미확인" }),
        el("span", { class: "small muted", text: card.name + (card.source ? " · " + card.source : "") })
      ]));
      if (!card.verified) {
        wrap.appendChild(el("div", { class: "small muted", text: "이 조건은 현재 검증 범위를 벗어납니다. 결과에 '재료 검증 미확인'이 표시됩니다." }));
      }
    } else {
      wrap.appendChild(el("div", { class: "control" }, [
        el("span", { class: "badge unknown", text: "확인 필요" }),
        el("span", { class: "small muted", text: "형상 파일만으로 재료를 확인할 수 없습니다." })
      ]));
    }
    return wrap;
  },

  directionPicker(load) {
    const wrap = el("div", { class: "field" });
    wrap.appendChild(el("span", { class: "field-label", text: "하중 방향" }));
    const grid = el("div", { class: "pictorial", role: "group", "aria-label": "하중 방향" });
    const options = [
      { label: "위에서 아래로", value: [0, 0, -1], arrow: "M21 4 L21 22 M15 16 L21 23 L27 16" },
      { label: "옆에서 안쪽으로", value: [0, 1, 0], arrow: "M4 15 L24 15 M18 9 L25 15 L18 21" },
      { label: "아래에서 위로", value: [0, 0, 1], arrow: "M21 26 L21 8 M15 14 L21 7 L27 14" },
      { label: "반대 방향", value: null, arrow: "M8 15 L34 15 M14 9 L8 15 L14 21 M28 9 L34 15 L28 21" }
    ];
    const current = Array.isArray(load.params.direction) ? load.params.direction : [0, 0, -1];
    for (const option of options) {
      const active = option.value && option.value.every((v, i) => v === current[i]);
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 42 30");
      svg.setAttribute("aria-hidden", "true");
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", "10"); rect.setAttribute("y", "18"); rect.setAttribute("width", "22");
      rect.setAttribute("height", "10"); rect.setAttribute("fill", "#d7dfe8");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", option.arrow);
      path.setAttribute("stroke", "#2456d8"); path.setAttribute("fill", "none"); path.setAttribute("stroke-width", "2");
      svg.appendChild(rect); svg.appendChild(path);
      const button = el("button", { type: "button", "aria-pressed": active ? "true" : "false" }, [svg, option.label]);
      button.addEventListener("click", () => {
        const value = option.value || current.map((v) => -v);
        App.graph.setParam(load.id, "direction", value);
        delete App.graph.meta.confirmations.load_direction;
        UI.renderAll();
      });
      grid.appendChild(button);
    }
    wrap.appendChild(grid);
    wrap.appendChild(el("p", { class: "small muted", text: directionSentence(load.params.direction, load.params.stroke) }));
    return wrap;
  },

  supportPicker(load) {
    const wrap = el("div", { class: "field" });
    wrap.appendChild(el("span", { class: "field-label", text: "고정·지지" }));
    const grid = el("div", { class: "pictorial", role: "group", "aria-label": "고정 방식" });
    const options = [
      { label: "바닥 고정", apply: { clamp_can_base: true, support: undefined } },
      { label: "지그 평면", apply: { support: "jig_plane", clamp_can_base: false } },
      { label: "V 블록", apply: { support: "v_block", clamp_can_base: false } },
      { label: "지정 없음", apply: { support: undefined, clamp_can_base: false } }
    ];
    for (const option of options) {
      const active = Object.keys(option.apply).every((k) => (load.params[k] || undefined) === (option.apply[k] || undefined));
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 42 30");
      svg.setAttribute("aria-hidden", "true");
      const body = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      body.setAttribute("x", "12"); body.setAttribute("y", "4"); body.setAttribute("width", "18");
      body.setAttribute("height", "18"); body.setAttribute("fill", "#d7dfe8");
      const ground = document.createElementNS("http://www.w3.org/2000/svg", "path");
      ground.setAttribute("d", "M4 24 L38 24 M8 24 L4 29 M16 24 L12 29 M24 24 L20 29 M32 24 L28 29");
      ground.setAttribute("stroke", "#08745b"); ground.setAttribute("stroke-width", "2"); ground.setAttribute("fill", "none");
      svg.appendChild(body); svg.appendChild(ground);
      const button = el("button", { type: "button", "aria-pressed": active ? "true" : "false" }, [svg, option.label]);
      button.addEventListener("click", () => {
        App.graph.batch("고정 조건", (m) => {
          const node = m.node(load.id);
          for (const key in option.apply) {
            if (option.apply[key] === undefined || option.apply[key] === false) delete node.params[key];
            else node.params[key] = option.apply[key];
            m.meta.provenance[load.id + "." + key] = "user";
          }
          delete m.meta.confirmations.support;
        });
        UI.renderAll();
      });
      grid.appendChild(button);
    }
    wrap.appendChild(grid);
    wrap.appendChild(el("p", { class: "small muted", text: "이 면은 해석 중 움직이지 않습니다." }));
    return wrap;
  },

  targetEditor() {
    const g = App.graph;
    const purpose = Caps.purpose(g.meta.purpose);
    const options = (purpose && purpose.outputs) || ["peak_load_N"];
    const target = g.meta.targets[0] || { metric_key: options[0], lower: null, upper: null, unit: "", inclusive: true };
    const wrap = el("div", {});
    const setTarget = (patch) => {
      g.batch("설계 목표", (m) => {
        const next = Object.assign({}, target, patch);
        const hasBound = next.lower !== null || next.upper !== null;
        m.meta.targets = hasBound ? [next] : [];
      });
      UI.renderAll();
    };
    wrap.appendChild(el("div", { class: "field" }, [
      el("label", { for: "targetMetric", text: "목표 지표" }),
      el("div", { class: "control" }, [
        el("select", { id: "targetMetric", onchange: (e) => setTarget({ metric_key: e.target.value }) },
          options.map((key) => el("option", { value: key, text: key, selected: key === target.metric_key })))
      ])
    ]));
    const unit = target.metric_key.indexOf("pressure") >= 0 ? "MPa" : (target.metric_key.indexOf("_N") > 0 ? "N" : "");
    wrap.appendChild(el("div", { class: "field" }, [
      el("label", { for: "targetLower", text: "하한" }),
      el("div", { class: "control" }, [
        el("input", {
          type: "text", id: "targetLower", inputmode: "decimal",
          value: target.lower === null || target.lower === undefined ? "" : String(target.lower),
          onchange: (e) => setTarget({ lower: e.target.value === "" ? null : Number(e.target.value), unit })
        }),
        el("span", { class: "unit", text: unit })
      ])
    ]));
    wrap.appendChild(el("div", { class: "field" }, [
      el("label", { for: "targetUpper", text: "상한" }),
      el("div", { class: "control" }, [
        el("input", {
          type: "text", id: "targetUpper", inputmode: "decimal",
          value: target.upper === null || target.upper === undefined ? "" : String(target.upper),
          onchange: (e) => setTarget({ upper: e.target.value === "" ? null : Number(e.target.value), unit })
        }),
        el("span", { class: "unit", text: unit })
      ])
    ]));
    if (target.lower !== null && target.upper !== null && Number(target.upper) <= Number(target.lower)) {
      wrap.appendChild(el("div", { class: "field-error", text: "목표 상한은 하한보다 커야 합니다." }));
    }
    wrap.appendChild(el("p", { class: "small muted", text: "목표를 비워도 해석은 가능합니다. 그 경우 '목표 이내' 판정만 표시되지 않습니다." }));
    return wrap;
  },

  confirmBlock() {
    const wrap = el("div", { class: "group" });
    const body = el("div", { class: "group-body" });
    const items = Guided.confirmations();
    for (const item of items) {
      const done = !!App.graph.meta.confirmations[item.key];
      const row = el("div", { class: "checkline" }, [
        el("input", {
          type: "checkbox", id: "confirm-" + item.key, checked: done,
          onchange: (e) => Guided.confirm(item.key, e.target.checked)
        }),
        el("label", { for: "confirm-" + item.key }, [
          el("b", { text: item.label }), " ",
          el("span", { class: "small muted", text: item.detail })
        ])
      ]);
      body.appendChild(row);
      body.appendChild(el("p", { class: "small muted", text: item.hint }));
    }
    body.appendChild(el("button", {
      class: "btn", id: "confirmAll", text: "이 조건을 확인했습니다",
      onclick: () => {
        App.graph.batch("필수 확인", (m) => { for (const item of items) m.meta.confirmations[item.key] = true; });
        UI.renderAll();
      }
    }));
    wrap.appendChild(el("summary", { text: "필수 확인" }));
    wrap.appendChild(body);
    return wrap;
  },

  /* -- 4단계: 실행 확인 ---------------------------------------------- */

  stepReview(panel) {
    panel.appendChild(el("h2", { class: "section-title", text: "4. 실행 확인" }));
    const g = App.graph;
    const geo = g.geometryNode();
    const material = g.byType("material")[0];
    const load = g.byType("pressure")[0] || g.byType("loading")[0];
    const purpose = Caps.purpose(g.meta.purpose);
    const table = el("table", { class: "kv" });
    const rows = [
      ["해석 목적", purpose ? purpose.title : "미선택"],
      ["형상", geo ? (NODE_TYPES.geometry.summary(geo).join(" · ")) : "미지정"],
      ["재료", material ? (Caps.material(material.params.key) ? Caps.material(material.params.key).name : String(material.params.key || "")) : "미지정"],
      ["하중", load ? NODE_TYPES[load.type].summary(load).join(" · ") : "미지정"],
      ["설계 목표", targetSentence(g.meta.targets[0])]
    ];
    for (const [key, value] of rows) table.appendChild(el("tr", {}, [el("th", { text: key }), el("td", { text: String(value) })]));
    panel.appendChild(table);

    const pending = Guided.pendingConfirmations();
    if (pending.length) {
      panel.appendChild(el("div", { class: "note warn" }, [
        "확인이 필요한 항목이 " + pending.length + "건 있습니다: " + pending.map((p) => p.label).join(", ")
      ]));
      panel.appendChild(el("button", { class: "btn", text: "3단계로 돌아가 확인", onclick: () => { App.step = 3; UI.renderAll(); } }));
    }

    panel.appendChild(el("h3", { class: "section-title", text: "사전 검사" }));
    panel.appendChild(this.preflightBlock());
    // 초보자의 기본 비교 동작은 첫 실행보다 쉬워야 한다(§1.2, §7.3): 결과가
    // 아직 없어도 여기서 두 번째 케이스를 만들 수 있다.
    panel.appendChild(el("button", {
      class: "btn", id: "btnCompareOne", text: "조건 하나 바꿔 비교",
      onclick: () => UI.showCompareDialog()
    }));
    panel.appendChild(el("p", { class: "small muted", text: "조건 하나를 바꿔 차이를 확인하세요." }));
  },

  preflightBlock() {
    const wrap = el("div", {});
    if (App.preflightState === "running") {
      wrap.appendChild(el("p", { class: "muted", text: "실행 조건을 확인하고 있습니다" }));
      return wrap;
    }
    if (App.preflightState === "error") {
      wrap.appendChild(el("div", { class: "note bad" }, [String((App.preflightError || {}).message || "사전 검사를 하지 못했습니다.")]));
      wrap.appendChild(el("button", { class: "btn small", text: "다시 확인", onclick: () => Runner.preflight(true) }));
      return wrap;
    }
    const preflight = App.preflight;
    if (!preflight) {
      wrap.appendChild(el("p", { class: "muted", text: "그래프를 완성하면 사전 검사가 실행됩니다." }));
      return wrap;
    }
    const combos = preflight.combinations || {};
    wrap.appendChild(el("p", {}, [
      el("b", { text: String(combos.total || 0) + "건" }), " — 메쉬 " + (combos.mesh || 0)
      + " × 재료 " + (combos.material || 0) + " × 하중 " + (combos.load || 0)
    ]));
    const list = el("div", {});
    for (const check of preflight.checks || []) {
      const cls = check.severity === "block" ? "bad" : (check.severity === "review" ? "warn" : "");
      const item = el("div", { class: "note " + cls }, [
        el("b", { text: check.severity === "block" ? "차단" : (check.severity === "review" ? "검토 필요" : "정보") }),
        " " + String(check.message)
      ]);
      if (check.detail) {
        const more = el("details", {}, [el("summary", { text: "상세 근거" }), el("p", { class: "small", text: String(check.detail) })]);
        item.appendChild(more);
      }
      if ((check.actions || []).includes("show_30min_preset")) {
        item.appendChild(el("button", { class: "btn small", text: "30분 목표 설정 보기", onclick: () => UI.showPresetEvidence() }));
      }
      list.appendChild(item);
    }
    wrap.appendChild(list);
    const cases = el("table", { class: "kv" });
    cases.appendChild(el("tr", {}, [el("th", { text: "케이스" }), el("th", { text: "변경값" })]));
    for (const item of preflight.cases || []) {
      const diff = Object.keys(item.changed_vs_first || {})
        .map((k) => k + ": " + JSON.stringify(item.changed_vs_first[k][0]) + " → " + JSON.stringify(item.changed_vs_first[k][1]));
      cases.appendChild(el("tr", {}, [
        el("td", { text: item.name }),
        el("td", { text: diff.length ? diff.join(", ") : "기준 조건" })
      ]));
    }
    wrap.appendChild(el("div", { class: "table-scroll" }, [cases]));
    return wrap;
  },

  nextButton(label, step) {
    return el("button", {
      class: "btn primary", text: label,
      onclick: () => { App.step = step; App.selectedNode = null; UI.renderAll(); }
    });
  },

  /* -- 노드 속성 ------------------------------------------------------ */

  renderNode(panel) {
    const node = App.graph.node(App.selectedNode);
    if (!node) { App.selectedNode = null; this.render(); return; }
    const def = NODE_TYPES[node.type];
    panel.appendChild(el("div", { class: "control" }, [
      el("button", { class: "btn small", text: "← 안내로 돌아가기", onclick: () => { App.selectedNode = null; UI.renderAll(); } })
    ]));
    panel.appendChild(el("h2", { class: "section-title", text: def ? def.title : node.type }));
    if (node.unknown) {
      panel.appendChild(el("div", { class: "note bad" }, ["이 버전이 모르는 노드입니다. 원본 값을 보존했고, 편집은 막았습니다."]));
      panel.appendChild(el("pre", { class: "small", text: JSON.stringify(node.params, null, 1) }));
      return;
    }
    const basic = def.fields.filter((f) => !f.advanced && (!f.show || f.show(node)));
    const advanced = def.fields.filter((f) => f.advanced && (!f.show || f.show(node)));
    for (const field of basic) panel.appendChild(field.type === "material" ? this.materialSelect(node, field.p) : this.fieldRow(node, field));
    if (advanced.length) {
      const details = el("details", { class: "group" }, [el("summary", { text: "고급 설정" })]);
      const body = el("div", { class: "group-body" });
      for (const field of advanced) body.appendChild(this.fieldRow(node, field));
      details.appendChild(body);
      panel.appendChild(details);
    }
  },

  fieldRow(node, field) {
    if (!field) return el("span");
    if (field.type === "preset") return this.presetRow(node, field);
    if (field.type === "material") return this.materialSelect(node, field.p);
    const wrap = el("div", { class: "field" });
    const id = "f-" + node.id + "-" + field.p;
    const value = node.params[field.p];
    const label = el("label", { for: id, text: field.label });
    wrap.appendChild(label);
    const control = el("div", { class: "control" });
    let input;
    if (field.type === "bool") {
      input = el("input", {
        type: "checkbox", id, checked: !!value,
        onchange: (e) => { App.graph.setParam(node.id, field.p, e.target.checked); UI.renderAll(); }
      });
    } else if (field.type === "select") {
      input = el("select", {
        id,
        onchange: (e) => { App.graph.setParam(node.id, field.p, e.target.value || undefined); UI.renderAll(); }
      }, field.opts.map((opt) => el("option", { value: opt, text: opt === "" ? "(없음)" : opt, selected: String(value || "") === opt })));
    } else {
      input = el("input", {
        type: "text", id,
        inputmode: field.type === "num" ? "decimal" : undefined,
        value: value === undefined || value === null ? "" : (Array.isArray(value) ? value.join(", ") : String(value)),
        onchange: (e) => {
          // 입력 중의 미완성 상태는 허용하고 blur/change에서만 검증한다(§6.2).
          const text = e.target.value.trim();
          if (text === "") { App.graph.setParam(node.id, field.p, undefined); UI.renderAll(); return; }
          if (field.type === "num") {
            const parsed = parseUnitNumber(text, field.unit);
            if (parsed === null) { this.markInvalid(wrap, "숫자를 입력하세요. 입력한 값은 그대로 두었습니다."); return; }
            App.graph.setParam(node.id, field.p, parsed.value);
            if (parsed.converted) announce(field.label + ": " + parsed.note);
          } else if (field.type === "vec") {
            const parts = text.split(",").map((v) => Number(v.trim()));
            if (parts.length !== 3 || parts.some((v) => Number.isNaN(v))) { this.markInvalid(wrap, "x, y, z 세 값을 쉼표로 입력하세요."); return; }
            App.graph.setParam(node.id, field.p, parts);
          } else {
            App.graph.setParam(node.id, field.p, text);
          }
          UI.renderAll();
        }
      });
    }
    control.appendChild(input);
    if (field.unit) control.appendChild(el("span", { class: "unit", text: field.unit }));
    wrap.appendChild(control);
    const provenance = App.graph.provenance(node.id, field.p);
    if (provenance) {
      const badge = el("button", {
        class: "badge " + provenance, type: "button", text: PROVENANCE_LABEL[provenance] || provenance,
        onclick: () => UI.showProvenance(provenance, node, field)
      });
      wrap.appendChild(badge);
    }
    if (field.help) wrap.appendChild(el("p", { class: "small muted", text: field.help }));
    return wrap;
  },

  presetRow(node, field) {
    const wrap = el("div", { class: "field" });
    const id = "f-" + node.id + "-preset";
    wrap.appendChild(el("label", { for: id, text: field.label }));
    const presets = Caps.presets(App.graph.meta.purpose);
    if (!presets.length) {
      wrap.appendChild(el("p", { class: "small muted", text: "이 목적에는 등록된 메쉬 프리셋이 없습니다." }));
      return wrap;
    }
    const current = node.params.mesh_preset || App.graph.meta.mesh_preset || (Caps.defaultPreset(App.graph.meta.purpose) || {}).id;
    wrap.appendChild(el("div", { class: "control" }, [
      el("select", {
        id,
        onchange: (e) => {
          App.graph.batch("메쉬 프리셋", (m) => {
            m.meta.mesh_preset = e.target.value;
            const solver = m.solver();
            if (solver) solver.params.mesh_preset = e.target.value;
          });
          UI.renderAll();
        }
      }, presets.map((p) => el("option", { value: p.id, text: p.label, selected: p.id === current })))
    ]));
    const preset = presets.find((p) => p.id === current);
    if (preset) {
      wrap.appendChild(el("div", { class: "small muted", text: String(preset.badge || "") }));
      if (preset.over_budget) wrap.appendChild(el("div", { class: "note warn" }, ["이 설정은 기본 시간 목표(30분)를 넘습니다."]));
    }
    return wrap;
  },

  markInvalid(wrap, message) {
    wrap.classList.add("invalid");
    const old = wrap.querySelector(".field-error");
    if (old) old.remove();
    wrap.appendChild(el("div", { class: "field-error", text: message }));
    announce(message);
  }
};

/** "0.5 mm"처럼 단위가 붙은 붙여넣기를 받아들인다. 다른 단위는 변환값을 알린다. */
function parseUnitNumber(text, unit) {
  const match = /^(-?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([a-zA-Z%]*)$/.exec(text);
  if (!match) return null;
  const value = Number(match[1]);
  if (Number.isNaN(value)) return null;
  const given = match[2].toLowerCase();
  if (!given || !unit || given === unit.toLowerCase()) return { value, converted: false };
  const factors = { "mm": { "cm": 10, "m": 1000, "in": 25.4 }, "mpa": { "kpa": 0.001, "bar": 0.1, "pa": 1e-6 }, "n": { "kn": 1000 } };
  const table = factors[unit.toLowerCase()];
  if (table && table[given]) {
    const converted = value * table[given];
    return { value: converted, converted: true, note: text + " → " + converted + " " + unit };
  }
  return { value, converted: false };
}

/* -------------------------------------------------------------- Runner */

const Runner = {
  timer: null,

  schedule() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.preflight(false), 400);
  },

  /** 그래프가 바뀌면 사전 검사를 다시 받는다. 입력이 같으면 다시 묻지 않는다. */
  async preflight(force) {
    const graph = App.graph.toJSON();
    const hash = stableStringify(graph);
    if (!force && hash === App.lastGraphHash && App.preflightState !== "error") return;
    App.lastGraphHash = hash;
    if (!App.graph.solver()) {
      App.preflight = null;
      App.preflightState = "idle";
      UI.renderRunBar();
      return;
    }
    App.preflightState = "running";
    App.preflight = null;   // 무효가 된 추정값을 실행 버튼 옆에 남기지 않는다(§8.1)
    UI.renderRunBar();
    try {
      const body = await Api.post("/api/preflights", { graph });
      if (stableStringify(App.graph.toJSON()) !== hash) return;  // 그 사이 또 바뀌었다
      App.preflight = body;
      App.preflightState = "ready";
      App.preflightError = null;
    } catch (error) {
      App.preflight = null;
      App.preflightState = "error";
      App.preflightError = error;
    }
    UI.renderRunBar();
    if (App.step === 4 && !App.selectedNode) Inspector.render();
  },

  async start() {
    const preflight = App.preflight;
    if (!preflight || !preflight.runnable) return;
    const button = $("btnStart");
    button.disabled = true;
    const key = "submit-" + preflight.input_hash + "-" + App.submitSeq;
    try {
      const body = await Api.post("/api/executions", {
        preflight_id: preflight.id,
        input_hash: preflight.input_hash,
        idempotency_key: key
      });
      App.submitSeq += 1;
      for (const item of body.executions || []) App.tracked.add(item.exec_id);
      saveTracked();
      announce((body.executions || []).length + "건을 대기열에 등록했습니다.");
      UI.openRunPanel();
      RunStore.poll(true);
    } catch (error) {
      if (error.code === "STALE_PREFLIGHT") {
        UI.toastError("입력이 바뀌어 다시 확인합니다. 수정한 조건은 다음 실행에 적용됩니다.");
        this.preflight(true);
      } else {
        UI.toastError(error.message || "해석을 시작하지 못했습니다.");
      }
    } finally {
      UI.renderRunBar();
    }
  }
};

/* ------------------------------------------------------------ RunStore */

function saveTracked() {
  try { localStorage.setItem("crushsim.tracked", JSON.stringify([...App.tracked])); } catch (err) { /* 저장 불가여도 동작한다 */ }
}
function loadTracked() {
  try {
    for (const id of JSON.parse(localStorage.getItem("crushsim.tracked") || "[]")) App.tracked.add(id);
  } catch (err) { /* 무시 */ }
}

const RunStore = {
  busy: false,
  start() {
    this.poll(true);
    // 5초 주기. 앞선 요청이 끝나기 전에는 겹쳐 보내지 않는다(§9.2).
    setInterval(() => this.poll(false), 5000);
  },
  async poll(force) {
    if (this.busy) return;
    if (!force && $("runPanel").hidden && !App.tracked.size) return;
    this.busy = true;
    try {
      const body = await Api.get("/api/executions");
      App.runs = body.items || [];
      // 재접속 복원: 이 탭이 제출한 실행이 목록에 없으면 id로 직접 물어본다(U22).
      for (const id of App.tracked) {
        if (!App.runs.some((r) => r.exec_id === id)) {
          try { App.runs.push(await Api.get("/api/executions/" + encodeURIComponent(id))); } catch (err) { /* 없어졌으면 넘어간다 */ }
        }
      }
      App.pollFailed = false;
      App.lastPollOk = new Date();
      await ResultPresenter.hydrate();
    } catch (error) {
      // 클라이언트의 통신 실패를 솔버 실패로 바꾸지 않는다(U23).
      App.pollFailed = true;
    } finally {
      this.busy = false;
      UI.renderRunPanel();
      UI.renderRunBar();
    }
  },
  async cancel(execId) {
    const run = App.runs.find((r) => r.exec_id === execId);
    const running = run && run.state === "running";
    const body = el("div", {}, [
      el("p", { text: running
        ? "현재 계산은 중단되며 완료 결과가 생성되지 않을 수 있습니다."
        : "대기열에서 제거합니다." })
    ]);
    Modal.open("해석을 중단할까요?", body, [
      { label: "계속 실행" },
      {
        label: "중단", primary: true,
        run: () => {
          Api.post("/api/executions/" + encodeURIComponent(execId) + "/cancel")
            .then(() => { announce("중단 요청을 처리하고 있습니다"); RunStore.poll(true); })
            .catch((error) => UI.toastError(error.message || "중단하지 못했습니다."));
        }
      }
    ]);
  }
};

/* ----------------------------------------------------- ResultPresenter */

const ResultPresenter = {
  async hydrate() {
    for (const run of App.runs) {
      if (run.state !== "completed" || App.results.has(run.exec_id) || run.legacy) continue;
      try { App.results.set(run.exec_id, await Api.get("/api/executions/" + encodeURIComponent(run.exec_id) + "/result")); }
      catch (error) { App.results.set(run.exec_id, null); }
    }
  },

  stateLine(run) {
    switch (run.state) {
      case "queued": {
        const position = run.queue_position || 1;
        const ahead = Math.max(0, position - 1);
        return "대기 " + position + "번째" + (ahead ? " · 앞선 해석 " + ahead + "건" : "");
      }
      case "running":
        return (STAGE_LABEL[run.stage] || "해석 중")
          + (run.engine_progress_pct != null ? " · 엔진 " + num(run.engine_progress_pct, 1) + "%" : "");
      case "cancelling": return "중단 요청을 처리하고 있습니다";
      case "completed": return "계산 완료";
      case "failed": return "해석이 완료되지 못했습니다";
      case "cancelled": return "사용자가 중단했습니다";
      case "interrupted": return "실행 환경 중단으로 완료되지 않았습니다";
      default: return String(run.state || "");
    }
  },

  targetBadge(result) {
    const map = {
      within: ["ok", "목표 범위 내"], below: ["warn", "하한 미달"], above: ["warn", "상한 초과"],
      none: ["", "목표 없음"], unavailable: ["warn", "판정 불가"]
    };
    const entry = map[result.target_status] || ["", "목표 없음"];
    return el("span", { class: "badge " + entry[0], text: entry[1] });
  },

  trustBlock(result) {
    const validation = result.validation || {};
    const validity = { valid: ["ok", "계산 유효"], invalid: ["bad", "계산 무효"], unknown: ["warn", "유효성 확인 불가"] }[validation.model_validity] || ["", "확인 불가"];
    const scope = { verified: ["ok", "검증 범위 내"], unverified: ["warn", "일부 조건 미검증"], out_of_scope: ["bad", "검증 범위 밖"] }[validation.scope_status] || ["", "근거 없음"];
    const wrap = el("div", { class: "control" }, [
      el("span", { class: "badge " + validity[0], text: validity[1] }),
      el("span", { class: "badge " + scope[0], text: scope[1] }),
      (validation.diagnostics || []).length ? el("span", { class: "badge warn", text: "진단 " + validation.diagnostics.length + "건" }) : null
    ]);
    const details = el("details", {}, [el("summary", { text: "검증 근거 보기" })]);
    for (const diag of validation.diagnostics || []) {
      details.appendChild(el("p", { class: "small", text: (diag.code || "") + " · " + (diag.message || "") }));
    }
    for (const ref of validation.references || []) {
      details.appendChild(el("p", { class: "small muted", text: typeof ref === "string" ? ref : JSON.stringify(ref) }));
    }
    return el("div", {}, [wrap, details]);
  },

  resultBlock(run) {
    const result = App.results.get(run.exec_id);
    if (!result) return el("div", { class: "small muted", text: "결과 정보를 아직 읽지 못했습니다." });
    const wrap = el("div", { class: "result-block" });
    wrap.appendChild(el("div", { class: "small muted", text: (result.case_name || run.exec_id) + " · " + ((run.timestamps || {}).finished || "") }));
    wrap.appendChild(el("p", { class: "judgement", text: (result.judgement || {}).sentence || "" }));
    if ((result.judgement || {}).caveat) wrap.appendChild(el("p", { class: "small muted", text: result.judgement.caveat }));
    wrap.appendChild(el("div", { class: "control" }, [this.targetBadge(result)]));
    wrap.appendChild(this.trustBlock(result));
    const scalars = (result.metrics || []).filter((m) => m.kind === "scalar").slice(0, 3);
    const grid = el("div", { class: "control" });
    for (const metric of scalars) {
      grid.appendChild(el("div", {}, [
        el("div", { class: "small muted", text: metric.label }),
        el("div", {}, [
          el("span", { class: "metric-value", text: metric.value === null || metric.value === undefined ? "–" : num(metric.value) }),
          el("span", { class: "metric-unit", text: metric.unit || "" })
        ]),
        metric.unavailable_reason ? el("div", { class: "small muted", text: this.reasonText(metric.unavailable_reason) }) : null
      ]));
    }
    wrap.appendChild(grid);
    const changed = Object.keys(result.changed_vs_first || {});
    if (changed.length) {
      wrap.appendChild(el("p", { class: "small muted", text: "변경 조건: " + changed.map((k) => k + " " + JSON.stringify(result.changed_vs_first[k][0]) + " → " + JSON.stringify(result.changed_vs_first[k][1])).join(", ") }));
    }
    return wrap;
  },

  reasonText(code) {
    return code === "NOT_REACHED_AT_MAX_PRESSURE"
      ? "최대 가압 압력까지 개방 기준에 도달하지 않았습니다."
      : String(code);
  },

  /* -- A/B 비교 (§10.5) ---------------------------------------------- */

  comparePanel() {
    const wrap = el("div", { class: "result-block" });
    const [idA, idB] = App.compare;
    const a = App.results.get(idA);
    const b = App.results.get(idB);
    wrap.appendChild(el("h3", {}, ["A/B 비교"]));
    if (!a || !b) {
      wrap.appendChild(el("p", { class: "muted", text: "완료된 결과 두 개를 고르면 비교합니다." }));
      return wrap;
    }
    wrap.appendChild(el("p", { class: "small" }, [
      el("span", { class: "badge", text: "A " + (a.case_name || idA) }), " ",
      el("span", { class: "badge", text: "B " + (b.case_name || idB) })
    ]));
    // 변경 조건 표: 같은 제출에서 나온 두 케이스는 changed_vs_first가 그 차이다.
    const diff = Object.keys(b.changed_vs_first || {}).length ? b.changed_vs_first : (a.changed_vs_first || {});
    const table = el("table", { class: "kv" });
    table.appendChild(el("tr", {}, [el("th", { text: "변경 조건" }), el("th", { text: "A → B" })]));
    const keys = Object.keys(diff);
    if (!keys.length) {
      table.appendChild(el("tr", {}, [el("td", { text: "정보 없음" }), el("td", { text: "두 실행이 같은 제출에서 나오지 않았습니다." })]));
    }
    for (const key of keys) {
      table.appendChild(el("tr", {}, [el("td", { text: key }), el("td", { text: JSON.stringify(diff[key][0]) + " → " + JSON.stringify(diff[key][1]) })]));
    }
    wrap.appendChild(el("div", { class: "table-scroll" }, [table]));

    const metrics = el("table", { class: "kv" });
    metrics.appendChild(el("tr", {}, [el("th", { text: "지표" }), el("th", { text: "A" }), el("th", { text: "B" }), el("th", { text: "차이" })]));
    for (const metricA of (a.metrics || []).filter((m) => m.kind === "scalar")) {
      const metricB = (b.metrics || []).find((m) => m.key === metricA.key);
      if (!metricB) continue;
      let diffText;
      if (metricA.value === null || metricA.value === undefined || metricB.value === null || metricB.value === undefined) {
        diffText = "결측 — 비교 불가";
      } else if (Number(metricA.value) === 0) {
        // 기준값이 0이면 상대 %가 무한대가 된다: 절대 차이만 보여준다(U34).
        diffText = "절대 " + num(Number(metricB.value) - Number(metricA.value)) + " " + (metricA.unit || "");
      } else {
        const rel = (Number(metricB.value) - Number(metricA.value)) / Number(metricA.value);
        diffText = (rel >= 0 ? "+" : "") + (rel * 100).toFixed(1) + " %";
      }
      metrics.appendChild(el("tr", {}, [
        el("td", { text: metricA.label }),
        el("td", { class: "num", text: metricA.value === null ? "–" : num(metricA.value) }),
        el("td", { class: "num", text: metricB.value === null ? "–" : num(metricB.value) }),
        el("td", { text: diffText })
      ]));
    }
    wrap.appendChild(el("div", { class: "table-scroll" }, [metrics]));

    // 같은 물리량·단위·정의의 곡선만 중첩한다(U33).
    const curvesA = (a.metrics || []).filter((m) => m.kind === "curve" && (m.points || []).length);
    let drawn = 0;
    for (const curveA of curvesA) {
      const curveB = (b.metrics || []).find((m) => m.key === curveA.key && m.kind === "curve");
      if (!curveB || !(curveB.points || []).length) continue;
      if (curveA.definition_id !== curveB.definition_id || curveA.x_unit !== curveB.x_unit || curveA.y_unit !== curveB.y_unit) {
        wrap.appendChild(el("p", { class: "small muted", text: curveA.label + ": 지표 정의나 단위가 달라 중첩하지 않았습니다." }));
        continue;
      }
      wrap.appendChild(el("h4", { text: curveA.label }));
      wrap.appendChild(this.chart(curveA, curveB));
      drawn += 1;
    }
    if (!drawn) wrap.appendChild(el("p", { class: "small muted", text: "중첩할 수 있는 같은 정의의 곡선이 없습니다." }));
    wrap.appendChild(el("p", { class: "small muted", text: "A는 실선(청색), B는 파선(자주)입니다. 축 범위는 두 결과를 함께 담도록 맞췄습니다." }));
    return wrap;
  },

  chart(curveA, curveB) {
    const canvas = el("canvas", { class: "ab-chart", width: 640, height: 220, role: "img",
      "aria-label": curveA.label + " A와 B 곡선 비교" });
    const ctx = canvas.getContext("2d");
    const all = curveA.points.concat(curveB.points);
    const xs = all.map((p) => p[0]);
    const ys = all.map((p) => p[1]);
    const x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    const y0 = Math.min.apply(null, ys.concat([0])), y1 = Math.max.apply(null, ys);
    const pad = { l: 54, r: 12, t: 12, b: 30 };
    const px = (x) => pad.l + ((x - x0) / (x1 - x0 || 1)) * (canvas.width - pad.l - pad.r);
    const py = (y) => canvas.height - pad.b - ((y - y0) / (y1 - y0 || 1)) * (canvas.height - pad.t - pad.b);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#d7dfe8";
    ctx.strokeRect(pad.l, pad.t, canvas.width - pad.l - pad.r, canvas.height - pad.t - pad.b);
    ctx.fillStyle = "#526176";
    ctx.font = "12px sans-serif";
    // 축 라벨은 값과 단위를 한 덩어리로 그린다(따로 그리면 오른쪽 끝에서 겹친다).
    const xMax = num(x1) + " " + String(curveA.x_unit || "");
    ctx.fillText(String(curveA.y_unit || ""), 6, pad.t + 10);
    ctx.fillText(num(y1), 6, pad.t + 24);
    ctx.fillText(num(y0), 6, canvas.height - pad.b);
    ctx.fillText(num(x0), pad.l, canvas.height - 10);
    ctx.fillText(xMax, canvas.width - pad.r - ctx.measureText(xMax).width, canvas.height - 10);
    const line = (points, color, dash) => {
      ctx.beginPath();
      ctx.setLineDash(dash);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.8;
      points.forEach((p, i) => { if (i === 0) ctx.moveTo(px(p[0]), py(p[1])); else ctx.lineTo(px(p[0]), py(p[1])); });
      ctx.stroke();
      ctx.setLineDash([]);
    };
    line(curveA.points, "#2456d8", []);
    line(curveB.points, "#7b3fb5", [6, 4]);
    return canvas;
  }
};

/* ------------------------------------------------------------------ UI */

const UI = {
  renderAll() {
    GraphView.render();
    Inspector.render();
    this.renderSteps();
    this.renderParts();
    this.renderPreview();
    this.renderRunBar();
    this.scheduleSave();
    Runner.schedule();
    $("btnUndo").disabled = !App.graph.canUndo();
    $("btnRedo").disabled = !App.graph.canRedo();
    $("btnToggleSettings").setAttribute("aria-pressed", App.showSettingsGroup ? "true" : "false");
    $("projectName").textContent = App.graph.name || "새 해석";
  },

  renderSteps() {
    const list = clear($("stepList"));
    const g = App.graph;
    const pending = Guided.pendingConfirmations().length;
    const steps = [
      { n: 1, label: "형상", sub: g.geometryNode() ? NODE_TYPES.geometry.summary(g.geometryNode())[0] : "미지정", done: !!g.geometryNode() },
      { n: 2, label: "알고 싶은 것", sub: Caps.purpose(g.meta.purpose) ? Caps.purpose(g.meta.purpose).title : "미선택", done: !!g.meta.purpose },
      { n: 3, label: "조건", sub: pending ? "확인 필요 " + pending + "건" : "확인 완료", done: !pending, attention: pending > 0 },
      { n: 4, label: "실행 확인", sub: App.preflight ? (App.preflight.combinations || {}).total + "건" : "사전 검사 대기", done: !!(App.preflight && App.preflight.runnable) }
    ];
    for (const step of steps) {
      const state = step.attention ? "attention" : (step.done ? "done" : "todo");
      const button = el("button", {
        class: "step-btn", dataset: { state },
        "aria-current": App.step === step.n && !App.selectedNode ? "step" : undefined,
        onclick: () => { App.step = step.n; App.selectedNode = null; UI.renderAll(); }
      }, [
        el("span", { class: "step-mark", text: state === "done" ? "✓" : (state === "attention" ? "!" : String(step.n)) }),
        el("span", {}, [el("b", { text: step.label }), el("span", { class: "step-sub", text: step.sub })])
      ]);
      list.appendChild(el("li", { class: "step-item" }, [button]));
    }
  },

  renderParts() {
    const list = clear($("partList"));
    const nodes = App.graph.chainNodes();
    if (!nodes.length) {
      list.appendChild(el("li", { class: "empty small", text: "형상을 먼저 정하세요" }));
      return;
    }
    for (const node of nodes) {
      const def = NODE_TYPES[node.type];
      list.appendChild(el("li", {}, [
        el("button", {
          type: "button", "aria-pressed": App.selectedNode === node.id ? "true" : "false",
          text: def ? def.title : node.type,
          onclick: () => { App.selectedNode = node.id; UI.renderAll(); }
        })
      ]));
    }
    for (const part of (App.asset && App.asset.parts) || []) {
      list.appendChild(el("li", {}, [
        el("button", { type: "button", text: "#" + part.index + " " + (part.role_suggestion || "부품") })
      ]));
    }
  },

  renderPreview() {
    const geo = App.graph.geometryNode();
    if (!geo) return;
    $("previewLabel").textContent = geo.params.kind === "step" ? "가져온 형상" : "형상 미리보기";
    if (geo.params.kind === "step") return;   // 자산 미리보기는 가져올 때 한 번 올린다
    Preview.showParametric(geo.params);
  },

  renderRunBar() {
    const estimate = $("runEstimate");
    const basis = $("runBasis");
    const notices = clear($("runNotices"));
    const button = $("btnStart");
    const preflight = App.preflight;
    const pending = Guided.pendingConfirmations();

    if (App.preflightState === "running") {
      estimate.textContent = "실행 조건을 확인하고 있습니다";
      basis.textContent = "";
      button.disabled = true;
    } else if (App.preflightState === "error") {
      estimate.textContent = "사전 검사를 하지 못했습니다";
      basis.textContent = String((App.preflightError || {}).message || "");
      button.disabled = true;
    } else if (!preflight) {
      estimate.textContent = "조건을 완성하면 예상 시간을 계산합니다";
      basis.textContent = "";
      button.disabled = true;
    } else {
      const total = (preflight.combinations || {}).total || 0;
      const est = preflight.estimate || {};
      if (est.available && est.total_min) {
        estimate.textContent = total + "건 · 예상 " + est.total_min[0] + "–" + est.total_min[1] + "분 · " + Caps.threads() + "코어";
        basis.textContent = String(est.basis || "") + (est.hardware ? " · " + est.hardware : "");
      } else {
        // 근거 없는 숫자를 만들지 않는다(§8.1, U17).
        estimate.textContent = total + "건 · 이 형상의 실행 시간은 아직 추정할 수 없습니다 · " + Caps.threads() + "코어";
        basis.textContent = String(est.reason || "");
      }
      const blocks = (preflight.checks || []).filter((c) => c.severity === "block");
      const reviews = (preflight.checks || []).filter((c) => c.severity === "review");
      if (blocks.length) {
        notices.appendChild(el("div", { class: "block-line" }, [
          blocks[0].code === "GRAPH_INCOMPLETE" ? "필수 부품의 연결을 확인해야 실행할 수 있습니다." : blocks[0].message,
          " ",
          el("button", { class: "btn small", text: "문제 위치 보기", onclick: () => { App.step = 4; App.selectedNode = blocks[0].node_id || null; UI.renderAll(); } })
        ]));
      }
      const overBudget = reviews.find((c) => c.code === "TIME_BUDGET_EXCEEDED");
      if (overBudget) {
        notices.appendChild(el("div", { class: "warn-line" }, [
          overBudget.message + " ",
          el("button", { class: "btn small", text: "30분 목표 설정 보기", onclick: () => this.showPresetEvidence() })
        ]));
      }
      button.disabled = !preflight.runnable || pending.length > 0;
    }

    if (pending.length) {
      notices.appendChild(el("div", { class: "warn-line" }, [
        "확인 필요 " + pending.length + "건 ",
        el("button", { class: "btn small", text: "확인하러 가기", onclick: () => { App.step = 3; App.selectedNode = null; UI.renderAll(); } })
      ]));
    }
    if (App.pollFailed) {
      notices.appendChild(el("div", { class: "warn-line" }, [
        "연결 확인 중 · 상태를 갱신하지 못했습니다. 마지막 확인: "
        + (App.lastPollOk ? timeOfDay(App.lastPollOk) : "없음") + " ",
        el("button", { class: "btn small", text: "다시 확인", onclick: () => RunStore.poll(true) })
      ]));
    }
    button.title = "현재 조건을 저장하고 대기열에 등록합니다";
  },

  renderRunPanel() {
    const panel = $("runPanel");
    if (panel.hidden) return;
    clear(panel);
    panel.appendChild(el("div", { class: "control" }, [
      el("h2", { class: "section-title", text: "실행" }),
      el("span", { class: "spacer" }),
      App.pollFailed
        ? el("span", { class: "badge warn", text: "연결 확인 중 · 마지막 확인 " + (App.lastPollOk ? timeOfDay(App.lastPollOk) : "없음") })
        : el("span", { class: "small muted", text: "5초마다 갱신 · 마지막 확인 " + (App.lastPollOk ? timeOfDay(App.lastPollOk) : "–") }),
      el("button", { class: "btn small", text: "닫기", onclick: () => this.closeRunPanel() })
    ]));
    if (!App.runs.length) {
      panel.appendChild(el("div", { class: "empty", text: "아직 실행한 해석이 없습니다. 조건을 확인하고 '해석 시작'을 누르세요." }));
      return;
    }
    const order = { running: 0, cancelling: 1, queued: 2, completed: 3, failed: 4, cancelled: 5, interrupted: 6 };
    const runs = App.runs.slice().sort((a, b) => (order[a.state] || 9) - (order[b.state] || 9));
    for (const run of runs) {
      const result = App.results.get(run.exec_id);
      const row = el("div", { class: "run-row" });
      row.appendChild(el("div", {}, [
        el("div", { class: "rname", text: run.case_name || run.exec_id }),
        run.legacy ? el("span", { class: "badge", text: "이전 런 · 스냅샷 정보 없음" }) : el("span", { class: "small muted", text: run.exec_id })
      ]));
      row.appendChild(el("div", {}, [el("span", {
        class: "badge " + (run.state === "completed" ? "ok" : (["failed", "interrupted"].includes(run.state) ? "bad" : (run.state === "cancelled" ? "" : "warn"))),
        text: ResultPresenter.stateLine(run)
      })]));
      const meta = el("div", { class: "rmeta" });
      if (result && result.judgement) meta.appendChild(el("div", { text: result.judgement.sentence }));
      else if (run.state === "failed") meta.appendChild(el("div", { text: "원인을 진단 로그에서 확인하세요." }));
      const timing = run.timing || {};
      if (timing.execution_seconds) meta.appendChild(el("div", { class: "small muted", text: "실행 " + num(timing.execution_seconds / 60) + "분 · 대기 " + num((timing.queue_seconds || 0) / 60) + "분" }));
      row.appendChild(meta);
      const actions = el("div", { class: "ractions" });
      const artifacts = run.artifacts || {};
      if (["queued", "running"].includes(run.state)) {
        actions.appendChild(el("button", { class: "btn small danger", text: "중단", onclick: () => RunStore.cancel(run.exec_id) }));
      }
      if (artifacts.viewer) actions.appendChild(el("a", { class: "btn small", href: artifacts.viewer, target: "_blank", rel: "noopener", text: "3D 뷰어" }));
      if (artifacts.report) actions.appendChild(el("a", { class: "btn small", href: artifacts.report, target: "_blank", rel: "noopener", text: "리포트" }));
      if (artifacts.csv) actions.appendChild(el("a", { class: "btn small", href: artifacts.csv, target: "_blank", rel: "noopener", text: "CSV" }));
      if (run.state === "completed") {
        actions.appendChild(el("button", { class: "btn small", text: "조건 하나 바꿔 비교", onclick: () => this.showCompareDialog() }));
        const checked = App.compare.includes(run.exec_id);
        actions.appendChild(el("label", { class: "small" }, [
          el("input", {
            type: "checkbox", checked,
            "aria-label": (run.case_name || run.exec_id) + " A/B 비교에 넣기",
            onchange: (e) => {
              if (e.target.checked) App.compare = App.compare.concat([run.exec_id]).slice(-2);
              else App.compare = App.compare.filter((id) => id !== run.exec_id);
              this.renderRunPanel();
            }
          }),
          " 비교"
        ]));
      }
      row.appendChild(actions);
      panel.appendChild(row);
      if (result) panel.appendChild(ResultPresenter.resultBlock(run));
    }
    if (App.compare.length === 2) panel.appendChild(ResultPresenter.comparePanel());
    else if (App.compare.length === 1) panel.appendChild(el("p", { class: "small muted", text: "조건 하나를 바꿔 차이를 확인하세요. 비교할 결과를 하나 더 고르세요." }));
  },

  openRunPanel() {
    $("runPanel").hidden = false;
    $("btnRunPanel").setAttribute("aria-expanded", "true");
    this.renderRunPanel();
    RunStore.poll(true);
  },
  closeRunPanel() {
    $("runPanel").hidden = true;
    $("btnRunPanel").setAttribute("aria-expanded", "false");
  },

  showStart() { $("startArea").hidden = false; },
  hideStart() { $("startArea").hidden = true; },

  toastError(message) {
    announce(message);
    GraphView.message(message);
  },

  showProvenance(kind, node, field) {
    const texts = {
      geometry: "형상 파일에서 읽은 값입니다. 값을 바꾸면 '직접 입력'으로 바뀝니다.",
      template: "검증된 템플릿의 추천값입니다.",
      user: "직접 입력한 값입니다.",
      unknown: "확인이 필요한 값입니다. 값이 없으면 해당 판정을 할 수 없습니다."
    };
    const body = el("div", {}, [
      el("p", { text: texts[kind] || "" }),
      el("p", { class: "small muted", text: (NODE_TYPES[node.type] ? NODE_TYPES[node.type].title : node.type) + " · " + field.label })
    ]);
    if (kind === "template") {
      const preset = Caps.defaultPreset(App.graph.meta.purpose);
      if (preset) body.appendChild(el("p", { class: "small", text: String(preset.badge || "") }));
    }
    Modal.open("이 값의 출처", body, [{ label: "닫기", primary: true }]);
  },

  showPresetEvidence() {
    const presets = Caps.presets(App.graph.meta.purpose);
    const body = el("div", {});
    if (!presets.length) body.appendChild(el("p", { text: "이 목적에는 등록된 프리셋이 없습니다." }));
    for (const preset of presets) {
      const evidence = preset.evidence || {};
      body.appendChild(el("div", { class: "note " + (preset.over_budget ? "warn" : "ok") }, [
        el("b", { text: preset.label }),
        el("p", { class: "small", text: String(preset.badge || "") }),
        el("p", { class: "small muted", text: "실측 " + (evidence.total_min || "?") + "분 · " + (evidence.hardware || "") + (evidence.vs_reference ? " · " + evidence.vs_reference : "") }),
        el("button", {
          class: "btn small", text: "이 설정 사용",
          onclick: () => {
            App.graph.batch("메쉬 프리셋", (m) => {
              m.meta.mesh_preset = preset.id;
              const solver = m.solver();
              if (solver) solver.params.mesh_preset = preset.id;
            });
            Modal.close();
            UI.renderAll();
          }
        })
      ]));
    }
    body.appendChild(el("p", { class: "small muted", text: "시간만 맞추려고 검증되지 않은 조대화를 자동 적용하지 않습니다. 바뀌는 값과 근거를 확인하고 고르세요." }));
    Modal.open("30분 목표 설정", body, [{ label: "닫기", primary: true }]);
  },

  /** 값 하나를 복제해 두 번째 케이스를 만든다(§7.3, U32). */
  showCompareDialog() {
    const g = App.graph;
    const candidates = [];
    for (const node of g.nodes) {
      const def = NODE_TYPES[node.type];
      if (!def || node.unknown) continue;
      for (const field of def.fields) {
        if (field.type !== "num") continue;
        if (node.params[field.p] === undefined) continue;
        candidates.push({ node, field });
      }
    }
    if (!candidates.length) {
      Modal.open("조건 하나 바꿔 비교", el("p", { text: "복제할 수치 조건이 없습니다." }), [{ label: "닫기", primary: true }]);
      return;
    }
    const select = el("select", { id: "cmpField" }, candidates.map((c, i) =>
      el("option", { value: String(i), text: (NODE_TYPES[c.node.type].title) + " · " + c.field.label + " (현재 " + num(c.node.params[c.field.p]) + ")" })));
    const input = el("input", { type: "text", id: "cmpValue", inputmode: "decimal", value: "" });
    const body = el("div", {}, [
      el("p", { text: "조건 하나를 바꿔 차이를 확인하세요. 두 번째 케이스만 추가되고 나머지 조건은 그대로입니다." }),
      el("div", { class: "field" }, [el("label", { for: "cmpField", text: "바꿀 값" }), el("div", { class: "control" }, [select])]),
      el("div", { class: "field" }, [el("label", { for: "cmpValue", text: "두 번째 값" }), el("div", { class: "control" }, [input])])
    ]);
    Modal.open("조건 하나 바꿔 비교", body, [
      { label: "취소" },
      {
        label: "비교안 만들기", primary: true,
        run: () => {
          const chosen = candidates[Number(select.value)];
          const value = Number(input.value);
          if (Number.isNaN(value) || input.value.trim() === "") { announce("두 번째 값을 숫자로 입력하세요."); return true; }
          Guided.compareOne(chosen.node.id, chosen.field.p, value);
        }
      }
    ]);
  },

  /* -- 저장 (§7.4) ---------------------------------------------------- */

  scheduleSave() {
    if (!App.graph.name || App.graph.readOnly) return;
    clearTimeout(App.saveTimer);
    App.saveTimer = setTimeout(() => this.save(), 1200);
  },

  async save() {
    const graph = App.graph;
    if (!graph.name || graph.readOnly) return;
    const state = $("saveState");
    state.dataset.state = "saving";
    state.textContent = "저장 중";
    const payload = Object.assign(graph.toJSON(), { base_revision: graph.meta.revision });
    try {
      const body = await Api.put("/api/graphs/" + encodeURIComponent(graph.name), payload);
      graph.meta.revision = body.revision;
      state.dataset.state = "saved";
      state.textContent = "저장됨";
    } catch (error) {
      state.dataset.state = "error";
      state.textContent = "저장 실패";
      if (error.code === "REVISION_CONFLICT") {
        Modal.open("다른 창에서 수정된 내용이 있습니다.",
          el("p", { text: "이 창의 편집을 덮어쓰지 않았습니다. 최신본을 열거나 다른 이름으로 저장하세요." }),
          [
            { label: "최신본 보기", run: () => this.openGraph(graph.name) },
            {
              label: "복사본 저장", primary: true,
              run: () => {
                graph.name = graph.name.replace(/\.json$/, "") + "_copy.json";
                graph.meta.revision = 0;
                this.save();
              }
            }
          ]);
      }
    }
  },

  async saveAs() {
    const input = el("input", { type: "text", id: "graphName", value: App.graph.name || "새_해석.json" });
    Modal.open("그래프 저장", el("div", { class: "field" }, [
      el("label", { for: "graphName", text: "파일 이름" }), el("div", { class: "control" }, [input])
    ]), [
      { label: "취소" },
      {
        label: "저장", primary: true,
        run: () => {
          let name = input.value.trim();
          if (!name) return true;
          if (!name.endsWith(".json")) name += ".json";
          App.graph.name = name;
          App.graph.meta.revision = 0;
          this.save();
          UI.renderAll();
        }
      }
    ]);
  },

  async openGraph(name) {
    try {
      const data = await Api.get("/api/graphs/" + encodeURIComponent(name));
      App.graph.load(data, name);
      App.asset = null;
      const refs = App.graph.meta.asset_refs || {};
      const first = Object.keys(refs)[0];
      if (first) await AssetImport.attachExisting(refs[first]);
      App.step = App.graph.meta.purpose ? 3 : 2;
      this.hideStart();
      this.renderAll();
      announce(name + "을(를) 열었습니다.");
    } catch (error) {
      this.toastError(error.message || "그래프를 열지 못했습니다.");
    }
  },

  async showOpenDialog(exampleOnly) {
    const body = el("div", { class: "list-choice" });
    try {
      const names = await Api.get("/api/graphs");
      for (const name of names) {
        body.appendChild(el("button", {
          class: "btn", text: name + (exampleOnly ? " · 예제 형상 · 예제 조건" : ""),
          onclick: () => { Modal.close(); this.openGraph(name); }
        }));
      }
      if (!exampleOnly) {
        const cases = await Api.get("/api/cases");
        body.appendChild(el("p", { class: "small muted", text: "기존 케이스 파일을 그래프로 가져올 수도 있습니다." }));
        for (const item of cases.filter((c) => !c.error)) {
          body.appendChild(el("button", {
            class: "btn", text: item.file + " · " + (item.name || ""),
            onclick: () => { Modal.close(); this.importCase(item.file); }
          }));
        }
      }
    } catch (error) {
      body.appendChild(el("p", { class: "muted", text: "목록을 읽지 못했습니다." }));
    }
    Modal.open(exampleOnly ? "예제로 둘러보기" : "저장한 그래프 열기", body, [{ label: "닫기" }]);
  },

  /** 기존 케이스 yaml을 그래프로 변환한다(의미 보존, 없는 정보는 미확인). */
  async importCase(file) {
    try {
      const y = await Api.get("/api/cases/" + encodeURIComponent(file) + "/raw");
      const g = App.graph;
      g.reset();
      g.name = null;
      g.batch("케이스 가져오기", (m) => {
        const geo = { id: m.newId(), type: "geometry", x: 20, y: 40, params: Object.assign({}, y.geometry) };
        const mesh = { id: m.newId(), type: "mesh", x: 300, y: 40, params: { target_size: (y.mesh || {}).target_size, vent_size: (y.mesh || {}).vent_size, imperfection_mm: (y.mesh || {}).imperfection_mm } };
        const mat = { id: m.newId(), type: "material", x: 20, y: 260, params: { key: (y.material || {}).key || y.material, eps_p_max: (y.material || {}).eps_p_max } };
        const isPressure = ((y.pressure || {}).peak > 0) && ((y.loading || {}).tool === "none");
        const load = isPressure
          ? { id: m.newId(), type: "pressure", x: 300, y: 300, params: Object.assign({}, y.pressure, { clamp_can_base: (y.loading || {}).clamp_can_base, brace_walls: (y.loading || {}).brace_walls }) }
          : { id: m.newId(), type: "loading", x: 300, y: 260, params: Object.assign({}, y.loading) };
        const contact = { id: m.newId(), type: "contact", x: 20, y: 440, params: { friction: (y.contact || {}).friction } };
        const solver = {
          id: m.newId(), type: "solver", x: 600, y: 40,
          params: {
            prefix: y.name, load_case: y.load_case, threads: (y.solver || {}).threads,
            animation_frames: (y.solver || {}).animation_frames,
            render: (y.output || {}).render, report: (y.output || {}).report
          }
        };
        const result = { id: m.newId(), type: "result", x: 600, y: 320, params: {} };
        const cap = (y.geometry || {}).closed_top ? { id: m.newId(), type: "cap", x: 160, y: 40, params: Object.assign({}, NODE_TYPES.cap.defaults) } : null;
        const vent = (y.geometry || {}).vent ? { id: m.newId(), type: "vent", x: 220, y: 40, params: Object.assign({}, y.geometry.vent) } : null;
        m.nodes.push(geo, mesh, mat, load, contact, solver, result);
        if (cap) m.nodes.push(cap);
        if (vent) m.nodes.push(vent);
        let tail = geo;
        if (cap) { m.edges.push({ from: tail.id, to: cap.id, port: "geom" }); tail = cap; }
        if (vent) { m.edges.push({ from: tail.id, to: vent.id, port: "geom" }); tail = vent; }
        m.edges.push({ from: tail.id, to: mesh.id, port: "geom" });
        m.edges.push({ from: mesh.id, to: solver.id, port: "mesh" });
        m.edges.push({ from: mat.id, to: solver.id, port: "mat" });
        m.edges.push({ from: load.id, to: solver.id, port: "load" });
        m.edges.push({ from: contact.id, to: solver.id, port: "contact" });
        m.edges.push({ from: solver.id, to: result.id, port: "runs" });
        if (vent) m.meta.purpose = "vent_burst";
        else if (y.load_case === "LC-1") m.meta.purpose = "axial_crush";
        else if (y.load_case === "LC-2") m.meta.purpose = "lateral_crush";
        for (const node of m.nodes) for (const key in node.params) m.meta.provenance[node.id + "." + key] = "user";
      });
      App.step = 3;
      this.hideStart();
      this.renderAll();
    } catch (error) {
      this.toastError(error.message || "케이스를 가져오지 못했습니다.");
    }
  }
};

/* ------------------------------------------------------- STEP 가져오기 */

const AssetImport = {
  poller: null,

  openDialog() {
    const limits = Caps.upload();
    const zone = el("div", { class: "dropzone", id: "dropzone", tabindex: "0", role: "button" }, [
      el("p", { text: ".stp 또는 .step 파일을 여기에 놓거나 아래에서 선택하세요." }),
      el("p", { class: "small", text: "허용 확장자 " + (limits.extensions || []).join(", ") + " · 최대 " + Math.round((limits.max_bytes || 0) / 1048576) + " MB" })
    ]);
    const status = el("p", { class: "small muted", id: "importStatus" });
    const body = el("div", {}, [
      zone,
      el("div", { class: "control" }, [
        el("button", { class: "btn", text: "파일 선택", onclick: () => $("stepFile").click() })
      ]),
      status,
      el("details", { class: "group" }, [
        el("summary", { text: "고급 가져오기" }),
        el("div", { class: "group-body" }, [
          el("p", { class: "small muted", text: "서버에 이미 있는 STEP 경로를 직접 지정합니다. 일반 사용에는 필요하지 않습니다." }),
          el("div", { class: "control" }, [
            el("input", { type: "text", id: "advStepPath", placeholder: "examples/step/cylin_can.stp" }),
            el("button", {
              class: "btn small", text: "적용",
              onclick: () => {
                const path = $("advStepPath").value.trim();
                if (!path) return;
                const g = App.graph;
                g.batch("STEP 경로 지정", (m) => {
                  let geo = m.geometryNode();
                  if (!geo) { geo = { id: m.newId(), type: "geometry", x: 20, y: 40, params: {} }; m.nodes.push(geo); }
                  geo.params.kind = "step";
                  geo.params.step_path = path;
                  m.meta.provenance[geo.id + ".step_path"] = "user";
                });
                Modal.close();
                UI.hideStart();
                UI.renderAll();
              }
            })
          ])
        ])
      ])
    ]);
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("over"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("over");
      if (e.dataTransfer.files && e.dataTransfer.files[0]) this.upload(e.dataTransfer.files[0]);
    });
    zone.addEventListener("click", () => $("stepFile").click());
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("stepFile").click(); } });
    Modal.open("STEP 파일 가져오기", body, [{ label: "닫기" }]);
  },

  setStatus(text) {
    const node = $("importStatus");
    if (node) node.textContent = text;
    announce(text);
  },

  async upload(file) {
    const limits = Caps.upload();
    const extensions = limits.extensions || [".stp", ".step"];
    const lower = (file.name || "").toLowerCase();
    if (!extensions.some((ext) => lower.endsWith(ext))) {
      this.setStatus("이 확장자는 가져올 수 없습니다. 허용: " + extensions.join(", "));
      return;
    }
    if (limits.max_bytes && file.size > limits.max_bytes) {
      this.setStatus("파일이 서버 제한(" + Math.round(limits.max_bytes / 1048576) + " MB)보다 큽니다.");
      return;
    }
    this.setStatus("전송 중…");
    const started = Date.now();
    let record;
    try {
      const body = await Api.upload("/api/assets", file);
      record = { id: body.asset_id, status: body.status };
    } catch (error) {
      // 가져오기에 실패해도 기존 편집은 그대로 둔다(U03).
      this.setStatus(error.message || "파일을 가져오지 못했습니다.");
      return;
    }
    this.setStatus("형상을 읽고 있습니다 · 경과 0초");
    clearInterval(this.poller);
    this.poller = setInterval(async () => {
      const seconds = Math.round((Date.now() - started) / 1000);
      try {
        const asset = await Api.get("/api/assets/" + encodeURIComponent(record.id));
        if (asset.status === "importing") {
          this.setStatus("형상을 읽고 있습니다 · 경과 " + seconds + "초");
          return;
        }
        clearInterval(this.poller);
        if (asset.status === "failed") {
          const error = asset.error || {};
          this.setStatus(String(error.message || "형상을 읽지 못했습니다."));
          return;
        }
        this.setStatus("부품 확인 · 준비됨");
        await this.adopt(asset);
        Modal.close();
      } catch (error) {
        clearInterval(this.poller);
        this.setStatus(error.message || "상태를 확인하지 못했습니다.");
      }
    }, 1000);
  },

  async adopt(asset) {
    // STEP만 넣고 끝내면 형상 노드 하나뿐인 그래프가 남아 다음 단계로 갈 수
    // 없다. 뼈대(메쉬·재료·하중·솔버·결과)를 먼저 세우고 형상만 STEP으로 바꾼다.
    if (!App.graph.solver()) Guided.startParametric("parametric_can");
    Guided.attachAsset(asset);
    UI.hideStart();
    App.step = 1;
    UI.renderAll();
    try {
      const preview = await Api.get("/api/assets/" + encodeURIComponent(asset.id) + "/preview");
      Preview.showAsset(preview);
    } catch (error) {
      Preview.note("미리보기 메쉬를 만들지 못했습니다. 치수와 부품 정보는 그대로 사용할 수 있습니다.");
    }
  },

  async attachExisting(assetId) {
    try {
      const asset = await Api.get("/api/assets/" + encodeURIComponent(assetId));
      App.asset = asset;
      const preview = await Api.get("/api/assets/" + encodeURIComponent(assetId) + "/preview");
      Preview.showAsset(preview);
    } catch (error) { /* 자산이 사라졌어도 그래프는 열린다 */ }
  }
};

/* ------------------------------------------------- 좁은 화면의 결과 목록 */

/** 768 px 아래에서는 제출을 지원하지 않는다. 그래도 완료된 해석의 결과·뷰어·
    리포트는 열 수 있어야 한다(§3.2: 모바일에서 결과 열람은 필수). */
async function renderMobileRuns() {
  const host = $("mobileRuns");
  if (!host) return;
  try {
    const body = await Api.get("/api/executions");
    const done = (body.items || []).filter((r) => r.state === "completed");
    clear(host);
    if (!done.length) {
      host.appendChild(el("p", { class: "muted", text: "완료된 해석이 아직 없습니다." }));
      return;
    }
    for (const run of done) {
      const artifacts = run.artifacts || {};
      host.appendChild(el("div", { class: "run-row" }, [
        el("div", { class: "rname", text: run.case_name || run.exec_id }),
        el("div", { class: "ractions" }, [
          artifacts.viewer ? el("a", { class: "btn small", href: artifacts.viewer, text: "3D 뷰어" }) : null,
          artifacts.report ? el("a", { class: "btn small", href: artifacts.report, text: "리포트" }) : null
        ])
      ]));
    }
  } catch (error) {
    clear(host).appendChild(el("p", { class: "muted", text: "결과 목록을 읽지 못했습니다." }));
  }
}

/* --------------------------------------------------------------- 분할선 */

function initSplitter() {
  const splitter = $("splitter");
  const center = $("center");
  const setSplit = (value) => {
    const top = Math.max(20, Math.min(85, value));
    center.style.setProperty("--split", top + "fr");
    center.style.setProperty("--split-rest", (100 - top) + "fr");
    splitter.setAttribute("aria-valuenow", String(Math.round(top)));
  };
  setSplit(60);
  let dragging = false;
  splitter.addEventListener("mousedown", () => { dragging = true; document.body.style.userSelect = "none"; });
  addEventListener("mouseup", () => { dragging = false; document.body.style.userSelect = ""; });
  addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const rect = center.getBoundingClientRect();
    setSplit(((event.clientY - rect.top) / rect.height) * 100);
    if (Preview.renderer) Preview.renderer.requestDraw();
  });
  splitter.addEventListener("keydown", (event) => {
    const current = parseFloat(getComputedStyle(center).getPropertyValue("--split")) || 60;
    if (event.key === "ArrowUp") { event.preventDefault(); setSplit(current - 5); }
    if (event.key === "ArrowDown") { event.preventDefault(); setSplit(current + 5); }
    if (Preview.renderer) Preview.renderer.requestDraw();
  });
}

/* ------------------------------------------------------------- 시작하기 */

async function boot() {
  const t0 = performance.now();
  loadTracked();
  Preview.init();
  initSplitter();

  $("startParametric").addEventListener("click", () => {
    const body = el("div", { class: "control" }, [
      el("button", { class: "btn", text: "원통형 캔", onclick: () => { Modal.close(); Guided.startParametric("parametric_can"); } }),
      el("button", { class: "btn", text: "각형 캔", onclick: () => { Modal.close(); Guided.startParametric("box_can"); } })
    ]);
    Modal.open("치수로 빠르게 시작", el("div", {}, [el("p", { text: "어떤 형태로 시작할까요? 치수는 다음 단계에서 바꿀 수 있습니다." }), body]), [{ label: "취소" }]);
  });
  $("startStep").addEventListener("click", () => AssetImport.openDialog());
  $("startOpenGraph").addEventListener("click", () => UI.showOpenDialog(false));
  $("startExample").addEventListener("click", () => UI.showOpenDialog(true));
  $("stepFile").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (file) AssetImport.upload(file);
  });

  $("btnOpen").addEventListener("click", () => UI.showOpenDialog(false));
  // 자동 저장은 이름이 정해진 뒤부터 돈다. 명시적 저장도 함께 둔다(§7.4).
  $("btnSave").addEventListener("click", () => (App.graph.name ? UI.save() : UI.saveAs()));
  $("btnNew").addEventListener("click", () => { App.graph.reset(); App.asset = null; App.preflight = null; UI.showStart(); UI.renderAll(); });
  $("btnHelp").addEventListener("click", () => {
    Modal.open("도움말", el("div", {}, [
      el("p", { text: "셀 형상을 넣고, 설계 방향을 빠르게 확인하세요." }),
      el("p", { class: "small muted", text: "왼쪽의 네 단계를 따라가면 첫 해석을 제출할 수 있습니다. 그래프에서 직접 노드를 고쳐도 같은 설정입니다." }),
      el("p", { class: "small muted", text: "단축키: Ctrl+Z 되돌리기 · Ctrl+Shift+Z 다시 실행 · Delete 선택 노드 삭제 (입력 중에는 동작하지 않습니다)." }),
      el("p", { class: "small muted", text: "목표: 4코어 노트북에서 1건 30분 이내. 검증된 조건에서 주요 지표의 정밀 해석 대비 오차 10% 이내를 목표로 합니다." })
    ]), [{ label: "닫기", primary: true }]);
  });
  $("btnUndo").addEventListener("click", () => { App.graph.undo(); UI.renderAll(); });
  $("btnRedo").addEventListener("click", () => { App.graph.redo(); UI.renderAll(); });
  $("btnToggleSettings").addEventListener("click", () => { App.showSettingsGroup = !App.showSettingsGroup; UI.renderAll(); });
  $("btnAddNode").addEventListener("click", () => {
    const body = el("div", { class: "list-choice" });
    for (const type in NODE_TYPES) {
      body.appendChild(el("button", {
        class: "btn", text: NODE_TYPES[type].title,
        onclick: () => { Modal.close(); App.graph.addNode(type, 60, 60); UI.renderAll(); }
      }));
    }
    Modal.open("노드 추가", body, [{ label: "닫기" }]);
  });
  $("btnFitView").addEventListener("click", () => Preview.fit());
  $("btnSelectedView").addEventListener("click", () => Preview.fit());
  $("btnStart").addEventListener("click", () => Runner.start());
  $("btnRunPanel").addEventListener("click", () => ($("runPanel").hidden ? UI.openRunPanel() : UI.closeRunPanel()));
  $("btnRunsFromSide").addEventListener("click", () => UI.openRunPanel());
  $("paneTabs").addEventListener("click", (event) => {
    const graphOn = $("paneGraph").dataset.tab === "on";
    $("paneGraph").dataset.tab = graphOn ? "off" : "on";
    $("panePreview").dataset.tab = graphOn ? "on" : "off";
    event.target.textContent = graphOn ? "그래프 보기" : "형상 보기";
    event.target.setAttribute("aria-pressed", graphOn ? "false" : "true");
  });
  $("inspectorToggle").addEventListener("click", (event) => {
    const panel = $("inspector");
    panel.hidden = !panel.hidden;
    event.target.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
  });
  $("graphCanvas").addEventListener("click", (event) => {
    if (event.target.id === "graphCanvas") { GraphView.setArmed(null); App.selectedNode = null; UI.renderAll(); }
  });
  addEventListener("keydown", (event) => {
    // 텍스트 입력 중에는 단축키를 가로채지 않는다(§7.2, §12.4).
    if (isTyping(event)) return;
    const meta = event.ctrlKey || event.metaKey;
    if (meta && event.key.toLowerCase() === "z") {
      event.preventDefault();
      if (event.shiftKey) App.graph.redo(); else App.graph.undo();
      UI.renderAll();
    } else if (event.key === "Delete" && App.selectedNode) {
      event.preventDefault();
      GraphView.removeNodeWithUndo(App.selectedNode);
      App.selectedNode = null;
      UI.renderAll();
    } else if (event.key === "Escape") {
      GraphView.setArmed(null);
    }
  });
  App.graph.on(() => { /* 변경은 renderAll에서 모아 처리한다 */ });

  try {
    await Caps.load();
  } catch (error) {
    UI.toastError("서버 기능 목록을 읽지 못했습니다. 화면을 새로 고쳐 보세요.");
  }
  renderMobileRuns();
  UI.renderAll();
  RunStore.start();
  const paint = performance.now() - t0;
  console.log("first paint (ms):", paint.toFixed(1));
  window.__firstPaintMs = paint;
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
