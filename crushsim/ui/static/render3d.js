/* render3d.js - UI_002 §1.1: the WebGL renderer shared by the workshop's
   geometry preview and the standalone result viewer.

   Dependency free on purpose (no build step, no CDN - UI_001 §13). The module
   is loaded with a plain <script> in the workshop and *inlined* into
   viewer.html by viewergen.py, so the exported viewer stays a single file that
   opens with no server (UI_001 §15).

   What lives here is only what both callers need: a GL context with one
   shader, named vertex groups the caller fills with its own triangles, a
   camera with orbit/pan/pinch, a clip plane, a wireframe pass and a
   translucent "original outline" overlay. What does NOT live here is anything
   that knows about the data: quad expansion, colour maps, part ids, frames.
   That stays in the viewer, because a renderer that knows what a vent foil is
   cannot be reused by the workshop preview.

   Usage:
       const r = Render3D.create(canvas, {clearColor: () => "#eef2f6"});
       if (!r) Render3D.showUnavailable(canvas);        // no WebGL here
       r.setMesh({positions, indices});                 // simple: one mesh
       r.setGroup(1, {positions, normals, colors, lines});   // or: per group
       r.attachControls();
       r.requestDraw();
*/
"use strict";
var Render3D = (function () {
  var VS =
    "attribute vec3 aPos; attribute vec3 aNrm; attribute vec4 aCol;\n" +
    "uniform mat4 uMVP; uniform mat3 uNrm; varying vec3 vN; varying vec4 vC; varying vec3 vW;\n" +
    "void main(){ gl_Position=uMVP*vec4(aPos,1.0); vN=uNrm*aNrm; vC=aCol; vW=aPos; }";
  var FS =
    "precision mediump float; varying vec3 vN; varying vec4 vC; varying vec3 vW;\n" +
    "uniform vec4 uClip; uniform float uClipOn; uniform vec3 uLight;\n" +
    "void main(){\n" +
    "  if(uClipOn>0.5 && dot(vW,uClip.xyz)>uClip.w) discard;\n" +
    "  float d=abs(dot(normalize(vN),normalize(uLight)));\n" +
    "  float shade=0.45+0.55*d;\n" +
    "  gl_FragColor=vec4(vC.rgb*shade, vC.a);\n" +
    "}";

  /* A phone reports devicePixelRatio 3: rendering a large model at 3x costs
     ~9x the fill of 1x for no visible gain on a small screen, and it is what
     makes the viewer stutter there. The backing store is capped at 2x. */
  var DEFAULT_MAX_DPR = 2;

  function hexToRgb(value) {
    var text = String(value || "").trim();
    if (text.charAt(0) === "#") text = text.slice(1);
    if (text.length === 3)
      text = text.charAt(0) + text.charAt(0) + text.charAt(1) + text.charAt(1) + text.charAt(2) + text.charAt(2);
    var n = parseInt(text, 16);
    if (text.length !== 6 || isNaN(n)) return [1, 1, 1];
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }

  function perspective(fov, asp, near, far) {
    var f = 1 / Math.tan(fov / 2);
    var o = new Float32Array(16);
    o[0] = f / asp;
    o[5] = f;
    o[10] = (far + near) / (near - far);
    o[11] = -1;
    o[14] = (2 * far * near) / (near - far);
    return o;
  }

  function mul(a, b) {
    var o = new Float32Array(16);
    for (var i = 0; i < 4; i++)
      for (var j = 0; j < 4; j++) {
        var s = 0;
        for (var k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k];
        o[i * 4 + j] = s;
      }
    return o;
  }

  /* ---------------------------------------------------------------- Renderer */

  function Renderer(canvas, gl, options) {
    this.canvas = canvas;
    this.gl = gl;
    this.options = options || {};
    this.maxDpr = this.options.maxDpr || DEFAULT_MAX_DPR;
    this.light = this.options.light || [0.4, 0.35, 0.85];
    this._groups = [];
    this._byId = {};
    this._overlay = null;
    this._showOverlay = false;
    this._wire = false;
    this._clip = { axis: -1, pos: 0.5, flip: false };
    this._drawQueued = false;
    this._detach = null;
    this._disposed = false;
    this.bounds = { min: [-1, -1, -1], max: [1, 1, 1] };
    /* The camera is public state on purpose: both callers drive it from their
       own view buttons and keyboard handlers. */
    this.camera = { rotX: -1.05, rotZ: 0.7, dist: 4, pan: [0, 0], center: [0, 0, 0], diag: 2 };
    this._home = null;

    this.program = this._program(VS, FS);
    this.loc = {
      aPos: gl.getAttribLocation(this.program, "aPos"),
      aNrm: gl.getAttribLocation(this.program, "aNrm"),
      aCol: gl.getAttribLocation(this.program, "aCol"),
      uMVP: gl.getUniformLocation(this.program, "uMVP"),
      uNrm: gl.getUniformLocation(this.program, "uNrm"),
      uClip: gl.getUniformLocation(this.program, "uClip"),
      uClipOn: gl.getUniformLocation(this.program, "uClipOn"),
      uLight: gl.getUniformLocation(this.program, "uLight")
    };
  }

  Renderer.prototype._program = function (vs, fs) {
    var gl = this.gl;
    var p = gl.createProgram();
    var pairs = [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]];
    for (var i = 0; i < pairs.length; i++) {
      var s = gl.createShader(pairs[i][0]);
      gl.shaderSource(s, pairs[i][1]);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      gl.attachShader(p, s);
    }
    gl.linkProgram(p);
    return p;
  };

  /* -- groups ------------------------------------------------------------- */

  Renderer.prototype._group = function (id) {
    var key = String(id);
    var group = this._byId[key];
    if (group) return group;
    var gl = this.gl;
    group = {
      id: key,
      vbo: gl.createBuffer(),
      nbo: gl.createBuffer(),
      cbo: gl.createBuffer(),
      lbo: gl.createBuffer(),
      nTri: 0,
      nLine: 0,
      visible: true,
      opacity: 1,
      order: this._groups.length
    };
    this._byId[key] = group;
    this._groups.push(group);
    return group;
  };

  /** Upload one group's triangle soup. Arrays are flat and already expanded:
      positions/normals are 3 floats per vertex, colors 4 (RGBA), lines pairs of
      vertices for the wireframe pass. Anything omitted keeps its old buffer. */
  Renderer.prototype.setGroup = function (id, data) {
    var gl = this.gl;
    var group = this._group(id);
    if (data.positions) {
      gl.bindBuffer(gl.ARRAY_BUFFER, group.vbo);
      gl.bufferData(gl.ARRAY_BUFFER, data.positions, gl.DYNAMIC_DRAW);
      group.nTri = data.positions.length / 3;
    }
    if (data.normals) {
      gl.bindBuffer(gl.ARRAY_BUFFER, group.nbo);
      gl.bufferData(gl.ARRAY_BUFFER, data.normals, gl.DYNAMIC_DRAW);
    }
    if (data.colors) {
      gl.bindBuffer(gl.ARRAY_BUFFER, group.cbo);
      gl.bufferData(gl.ARRAY_BUFFER, data.colors, gl.DYNAMIC_DRAW);
    }
    if (data.lines) {
      gl.bindBuffer(gl.ARRAY_BUFFER, group.lbo);
      gl.bufferData(gl.ARRAY_BUFFER, data.lines, gl.DYNAMIC_DRAW);
      group.nLine = data.lines.length / 3;
    }
    if (data.visible !== undefined) group.visible = !!data.visible;
    if (data.opacity !== undefined) group.opacity = data.opacity;
    return group;
  };

  Renderer.prototype.setGroupState = function (id, state) {
    var group = this._byId[String(id)];
    if (!group) return;
    if (state.visible !== undefined) group.visible = !!state.visible;
    if (state.opacity !== undefined) group.opacity = state.opacity;
  };

  Renderer.prototype.hasGroup = function (id) {
    return !!this._byId[String(id)];
  };

  Renderer.prototype.clearGroups = function () {
    var gl = this.gl;
    for (var i = 0; i < this._groups.length; i++) {
      var g = this._groups[i];
      gl.deleteBuffer(g.vbo);
      gl.deleteBuffer(g.nbo);
      gl.deleteBuffer(g.cbo);
      gl.deleteBuffer(g.lbo);
    }
    this._groups = [];
    this._byId = {};
  };

  /* -- the simple path: one indexed mesh ---------------------------------- */

  /** Flat-shaded triangle soup from {positions:[x,y,z,...], indices:[i,j,k,...]}.

      This is the shape ``GET /api/assets/{id}/preview`` returns, so the
      workshop can hand the response straight over. ``faceColors`` (3 floats per
      triangle) colours individual triangles - used to highlight one part of an
      imported STEP without re-uploading the mesh. */
  Renderer.prototype.setMesh = function (mesh) {
    var positions = mesh.positions;
    var indices = mesh.indices;
    var color = mesh.color || [0.62, 0.66, 0.72];
    var alpha = mesh.opacity === undefined ? 1 : mesh.opacity;
    var triangles = Math.floor(indices.length / 3);
    var V = new Float32Array(triangles * 9);
    var N = new Float32Array(triangles * 9);
    var C = new Float32Array(triangles * 12);
    var L = mesh.edges === false ? null : new Float32Array(triangles * 18);
    var vp = 0, cp = 0, lp = 0;
    var min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (var t = 0; t < triangles; t++) {
      var i0 = indices[t * 3] * 3, i1 = indices[t * 3 + 1] * 3, i2 = indices[t * 3 + 2] * 3;
      var ax = positions[i0], ay = positions[i0 + 1], az = positions[i0 + 2];
      var bx = positions[i1], by = positions[i1 + 1], bz = positions[i1 + 2];
      var cx = positions[i2], cy = positions[i2 + 1], cz = positions[i2 + 2];
      var ux = bx - ax, uy = by - ay, uz = bz - az;
      var wx = cx - ax, wy = cy - ay, wz = cz - az;
      var nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
      var nl = Math.hypot(nx, ny, nz) || 1;
      nx /= nl; ny /= nl; nz /= nl;
      var r = color[0], g = color[1], b = color[2];
      if (mesh.faceColors) {
        r = mesh.faceColors[t * 3];
        g = mesh.faceColors[t * 3 + 1];
        b = mesh.faceColors[t * 3 + 2];
      }
      var tri = [ax, ay, az, bx, by, bz, cx, cy, cz];
      for (var k = 0; k < 3; k++) {
        for (var c2 = 0; c2 < 3; c2++) {
          var value = tri[k * 3 + c2];
          V[vp + c2] = value;
          N[vp + c2] = c2 === 0 ? nx : c2 === 1 ? ny : nz;
          if (value < min[c2]) min[c2] = value;
          if (value > max[c2]) max[c2] = value;
        }
        vp += 3;
        C[cp] = r; C[cp + 1] = g; C[cp + 2] = b; C[cp + 3] = alpha;
        cp += 4;
      }
      if (L) {
        var edges = [0, 1, 1, 2, 2, 0];
        for (var e = 0; e < 6; e++) {
          var base = edges[e] * 3;
          L[lp] = tri[base]; L[lp + 1] = tri[base + 1]; L[lp + 2] = tri[base + 2];
          lp += 3;
        }
      }
    }
    // edges:false면 **빈** 선 버퍼를 올린다. 넘기지 않으면 직전 메쉬의 선
    // 버퍼가 그대로 남아, 새 메쉬 위에 예전 와이어프레임이 겹쳐 그려졌다.
    this.setGroup("mesh", { positions: V, normals: N, colors: C, lines: L || new Float32Array(0), opacity: alpha });
    if (isFinite(min[0])) this.setBounds(min, max);
    return { triangles: triangles, bounds: { min: min, max: max } };
  };

  /* -- overlay (the undeformed outline) ----------------------------------- */

  Renderer.prototype.setOverlay = function (data) {
    var gl = this.gl;
    if (!data) {
      this._overlay = null;
      return;
    }
    var overlay = this._overlay || {
      vbo: gl.createBuffer(),
      lbo: gl.createBuffer(),
      nTri: 0,
      nLine: 0
    };
    if (data.positions) {
      gl.bindBuffer(gl.ARRAY_BUFFER, overlay.vbo);
      gl.bufferData(gl.ARRAY_BUFFER, data.positions, gl.STATIC_DRAW);
      overlay.nTri = data.positions.length / 3;
    }
    if (data.lines) {
      gl.bindBuffer(gl.ARRAY_BUFFER, overlay.lbo);
      gl.bufferData(gl.ARRAY_BUFFER, data.lines, gl.STATIC_DRAW);
      overlay.nLine = data.lines.length / 3;
    }
    overlay.faceColor = data.faceColor || [0.45, 0.5, 0.6, 0.25];
    overlay.lineColor = data.lineColor || [0.4, 0.45, 0.55, 0.55];
    this._overlay = overlay;
  };

  Renderer.prototype.showOverlay = function (on) {
    this._showOverlay = !!on;
  };

  Renderer.prototype.hasOverlay = function () {
    return !!this._overlay;
  };

  /* -- view state --------------------------------------------------------- */

  Renderer.prototype.setWireframe = function (on) {
    this._wire = !!on;
  };

  Renderer.prototype.wireframe = function () {
    return this._wire;
  };

  /** axis -1 = off, else 0|1|2; pos is 0..1 across the bounding box. */
  Renderer.prototype.setClip = function (clip) {
    if (clip.axis !== undefined) this._clip.axis = clip.axis;
    if (clip.pos !== undefined) this._clip.pos = clip.pos;
    if (clip.flip !== undefined) this._clip.flip = !!clip.flip;
  };

  Renderer.prototype.setBounds = function (min, max) {
    this.bounds = { min: [min[0], min[1], min[2]], max: [max[0], max[1], max[2]] };
    this.camera.center = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
    this.camera.diag = Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) || 1;
  };

  /** Frame the whole model. ``factor`` scales the distance (1.9 is the viewer's
      default framing, which leaves a margin around the can). */
  Renderer.prototype.fit = function (factor) {
    this.camera.dist = this.camera.diag * (factor || 1.9);
    this.camera.pan = [0, 0];
    if (!this._home) this._home = { rotX: this.camera.rotX, rotZ: this.camera.rotZ };
  };

  var VIEWS = {
    iso: { rotX: -1.05, rotZ: 0.7 },
    front: { rotX: -Math.PI / 2, rotZ: 0 },
    top: { rotX: 0, rotZ: 0 },
    side: { rotX: -Math.PI / 2, rotZ: Math.PI / 2 }
  };

  Renderer.prototype.setView = function (name, factor) {
    var view = VIEWS[name] || VIEWS.iso;
    this.camera.rotX = view.rotX;
    this.camera.rotZ = view.rotZ;
    this.camera.pan = [0, 0];
    this.camera.dist = this.camera.diag * (factor || 1.9);
  };

  Renderer.prototype.orbit = function (dx, dy) {
    this.camera.rotZ += dx;
    /* Clamped to the lower hemisphere so the model never flips upside down
       mid-drag, which is disorienting when a part is being pointed at. */
    this.camera.rotX = Math.max(-Math.PI, Math.min(0, this.camera.rotX + dy));
  };

  Renderer.prototype.pan = function (dx, dy) {
    var s = this.camera.dist * 0.0011;
    this.camera.pan[0] += dx * s;
    this.camera.pan[1] -= dy * s;
  };

  Renderer.prototype.zoom = function (factor) {
    var diag = this.camera.diag;
    this.camera.dist = Math.max(diag * 0.25, Math.min(diag * 6, this.camera.dist * factor));
  };

  Renderer.prototype.viewMatrix = function () {
    var cam = this.camera;
    var cz = Math.cos(cam.rotZ), sz = Math.sin(cam.rotZ);
    var cx = Math.cos(cam.rotX), sx = Math.sin(cam.rotX);
    var T = function (x, y, z) {
      return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]);
    };
    var RZ = new Float32Array([cz, sz, 0, 0, -sz, cz, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
    var RX = new Float32Array([1, 0, 0, 0, 0, cx, sx, 0, 0, -sx, cx, 0, 0, 0, 0, 1]);
    var M = T(-cam.center[0], -cam.center[1], -cam.center[2]);
    M = mul(RZ, M);
    M = mul(RX, M);
    return mul(T(cam.pan[0], cam.pan[1], -cam.dist), M);
  };

  /* -- drawing ------------------------------------------------------------ */

  Renderer.prototype.resize = function () {
    var dpr = Math.min(window.devicePixelRatio || 1, this.maxDpr);
    var w = Math.round(this.canvas.clientWidth * dpr);
    var h = Math.round(this.canvas.clientHeight * dpr);
    if (w > 0 && h > 0 && (this.canvas.width !== w || this.canvas.height !== h)) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    return { width: this.canvas.width, height: this.canvas.height };
  };

  Renderer.prototype._clearColor = function () {
    var value = this.options.clearColor;
    if (typeof value === "function") value = value();
    if (!value) return [0.93, 0.95, 0.97];
    if (typeof value === "string") return hexToRgb(value);
    return value;
  };

  Renderer.prototype.draw = function () {
    if (this._disposed) return;
    var gl = this.gl;
    var size = this.resize();
    var w = size.width, h = size.height;
    if (!w || !h) return;
    gl.viewport(0, 0, w, h);
    var bg = this._clearColor();
    gl.clearColor(bg[0], bg[1], bg[2], 1);
    gl.enable(gl.DEPTH_TEST);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(this.program);
    var V = this.viewMatrix();
    var diag = this.camera.diag;
    var MVP = mul(perspective(0.6, w / h, diag * 0.02, diag * 10), V);
    gl.uniformMatrix4fv(this.loc.uMVP, false, MVP);
    /* normal matrix ~ the rotation part of the view matrix */
    gl.uniformMatrix3fv(
      this.loc.uNrm,
      false,
      new Float32Array([V[0], V[1], V[2], V[4], V[5], V[6], V[8], V[9], V[10]])
    );
    gl.uniform3f(this.loc.uLight, this.light[0], this.light[1], this.light[2]);
    if (this._clip.axis >= 0) {
      var n = [0, 0, 0];
      n[this._clip.axis] = this._clip.flip ? -1 : 1;
      var lo = this.bounds.min[this._clip.axis], hi = this.bounds.max[this._clip.axis];
      var d = lo + (hi - lo) * this._clip.pos;
      if (this._clip.flip) d = -d;
      gl.uniform4f(this.loc.uClip, n[0], n[1], n[2], d);
      gl.uniform1f(this.loc.uClipOn, 1);
    } else {
      gl.uniform1f(this.loc.uClipOn, 0);
    }

    /* Opaque groups first, translucent ones after and back to front by
       opacity: with depth writes off a translucent part drawn first hides
       everything behind it. */
    var order = this._groups.filter(function (g) {
      return g.visible && g.nTri > 0;
    });
    order.sort(function (a, b) {
      return b.opacity - a.opacity || a.order - b.order;
    });
    for (var i = 0; i < order.length; i++) this._drawGroup(order[i]);
    gl.depthMask(true);
    if (this._showOverlay && this._overlay) this._drawOverlay();
    if (typeof this.options.onDraw === "function") this.options.onDraw(this);
  };

  Renderer.prototype._drawGroup = function (group) {
    var gl = this.gl, loc = this.loc;
    var transparent = group.opacity < 0.999;
    if (transparent) {
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.depthMask(false);
    } else {
      gl.disable(gl.BLEND);
      gl.depthMask(true);
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, group.vbo);
    gl.enableVertexAttribArray(loc.aPos);
    gl.vertexAttribPointer(loc.aPos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, group.nbo);
    gl.enableVertexAttribArray(loc.aNrm);
    gl.vertexAttribPointer(loc.aNrm, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, group.cbo);
    gl.enableVertexAttribArray(loc.aCol);
    gl.vertexAttribPointer(loc.aCol, 4, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, group.nTri);
    if (this._wire && group.nLine) {
      var wire = this.options.wireColor || [0.25, 0.28, 0.33];
      gl.disableVertexAttribArray(loc.aCol);
      gl.vertexAttrib4f(loc.aCol, wire[0], wire[1], wire[2], Math.min(0.5, group.opacity + 0.2));
      gl.disableVertexAttribArray(loc.aNrm);
      gl.vertexAttrib3f(loc.aNrm, 0, 0, 1);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.bindBuffer(gl.ARRAY_BUFFER, group.lbo);
      gl.enableVertexAttribArray(loc.aPos);
      gl.vertexAttribPointer(loc.aPos, 3, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, group.nLine);
      gl.enableVertexAttribArray(loc.aNrm);
      gl.enableVertexAttribArray(loc.aCol);
    }
  };

  Renderer.prototype._drawOverlay = function () {
    var gl = this.gl, loc = this.loc, overlay = this._overlay;
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.depthMask(false);
    gl.bindBuffer(gl.ARRAY_BUFFER, overlay.vbo);
    gl.enableVertexAttribArray(loc.aPos);
    gl.vertexAttribPointer(loc.aPos, 3, gl.FLOAT, false, 0, 0);
    gl.disableVertexAttribArray(loc.aNrm);
    gl.vertexAttrib3f(loc.aNrm, 0, 0, 1);
    gl.disableVertexAttribArray(loc.aCol);
    var fc = overlay.faceColor;
    gl.vertexAttrib4f(loc.aCol, fc[0], fc[1], fc[2], fc[3]);
    gl.drawArrays(gl.TRIANGLES, 0, overlay.nTri);
    if (overlay.nLine) {
      var lc = overlay.lineColor;
      gl.vertexAttrib4f(loc.aCol, lc[0], lc[1], lc[2], lc[3]);
      gl.bindBuffer(gl.ARRAY_BUFFER, overlay.lbo);
      gl.vertexAttribPointer(loc.aPos, 3, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, overlay.nLine);
    }
    gl.enableVertexAttribArray(loc.aNrm);
    gl.enableVertexAttribArray(loc.aCol);
    gl.depthMask(true);
  };

  Renderer.prototype.requestDraw = function () {
    var self = this;
    if (this._drawQueued || this._disposed) return;
    this._drawQueued = true;
    requestAnimationFrame(function () {
      self._drawQueued = false;
      self.draw();
    });
  };

  /* -- pointer controls --------------------------------------------------- */

  /** Orbit / pan / pinch on the canvas.

      Pointer events cover mouse, pen and touch alike. Two live pointers are a
      pinch: their distance zooms and their midpoint pans, which is the only
      way to zoom or pan on a touchscreen (there is no wheel and no shift key).
      Returns a function that removes every listener again. */
  Renderer.prototype.attachControls = function (opts) {
    if (this._detach) this._detach();
    var self = this;
    var canvas = this.canvas;
    var options = opts || {};
    var pointers = {};
    var count = 0;
    var drag = null;
    var pinch = null;

    function pinchState() {
      var list = [];
      for (var key in pointers) if (pointers.hasOwnProperty(key)) list.push(pointers[key]);
      var a = list[0], b = list[1];
      return { d: Math.hypot(a.x - b.x, a.y - b.y), cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2 };
    }
    function down(e) {
      if (!pointers[e.pointerId]) count++;
      pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (canvas.setPointerCapture) canvas.setPointerCapture(e.pointerId);
      if (count === 2) {
        drag = null;
        var st = pinchState();
        pinch = { d: st.d, cx: st.cx, cy: st.cy, dist: self.camera.dist };
      } else if (count === 1) {
        drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 2 };
        canvas.classList.add("dragging");
      }
    }
    function move(e) {
      if (!pointers[e.pointerId]) return;
      pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (pinch && count === 2) {
        var now = pinchState();
        self.camera.dist = Math.max(
          self.camera.diag * 0.25,
          Math.min(self.camera.diag * 6, pinch.dist * (pinch.d / Math.max(now.d, 1e-6)))
        );
        self.pan(now.cx - pinch.cx, now.cy - pinch.cy);
        pinch.cx = now.cx;
        pinch.cy = now.cy;
        pinch.d = now.d;
        pinch.dist = self.camera.dist;
        self.requestDraw();
        return;
      }
      if (!drag) return;
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX;
      drag.y = e.clientY;
      if (drag.pan) self.pan(dx, dy);
      else self.orbit(dx * 0.008, dy * 0.008);
      self.requestDraw();
    }
    function end(e) {
      if (pointers[e.pointerId]) {
        delete pointers[e.pointerId];
        count--;
      }
      if (count < 2) pinch = null;
      if (count === 0) {
        drag = null;
        canvas.classList.remove("dragging");
      } else if (!pinch) {
        /* one finger left after a pinch: keep orbiting from where it is */
        for (var key in pointers)
          if (pointers.hasOwnProperty(key)) drag = { x: pointers[key].x, y: pointers[key].y, pan: false };
      }
    }
    function wheel(e) {
      /* The page must still scroll around the canvas (UI_001 §7.2), so zoom
         only claims the wheel over the canvas itself. */
      e.preventDefault();
      self.zoom(Math.exp(e.deltaY * 0.0012));
      self.requestDraw();
    }
    function noop(e) {
      e.preventDefault();
    }
    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    if (options.wheel !== false) canvas.addEventListener("wheel", wheel, { passive: false });
    canvas.addEventListener("contextmenu", noop);
    /* iOS Safari still fires its own page zoom on a double tap over a canvas. */
    canvas.addEventListener("dblclick", noop);
    this._detach = function () {
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", end);
      canvas.removeEventListener("pointercancel", end);
      canvas.removeEventListener("wheel", wheel);
      canvas.removeEventListener("contextmenu", noop);
      canvas.removeEventListener("dblclick", noop);
      self._detach = null;
    };
    return this._detach;
  };

  Renderer.prototype.dispose = function () {
    if (this._detach) this._detach();
    this.clearGroups();
    var gl = this.gl;
    if (this._overlay) {
      gl.deleteBuffer(this._overlay.vbo);
      gl.deleteBuffer(this._overlay.lbo);
      this._overlay = null;
    }
    if (this.program) gl.deleteProgram(this.program);
    this.program = null;
    this._disposed = true;
  };

  /* ------------------------------------------------------------- factories */

  /** A WebGL renderer on ``canvas``, or **null** when the browser has no WebGL.

      Some in-app preview webviews (a file opened inside a messaging or mail
      app, iOS Quick Look) render HTML but refuse WebGL. Returning null instead
      of throwing lets the caller show the fallback and keep the rest of the
      page - metrics, curves and links - working (UI_001 §10.3). */
  function create(canvas, options) {
    if (!canvas || !canvas.getContext) return null;
    var attrs = { antialias: true, preserveDrawingBuffer: !!(options && options.preserveDrawingBuffer) };
    var gl = null;
    try {
      gl = canvas.getContext("webgl", attrs) || canvas.getContext("experimental-webgl", attrs);
    } catch (err) {
      gl = null;
    }
    if (!gl) return null;
    try {
      return new Renderer(canvas, gl, options || {});
    } catch (err) {
      return null;
    }
  }

  var UNAVAILABLE_HTML =
    "<div><b>3D 뷰를 표시할 수 없습니다</b><br>" +
    "이 화면은 WebGL을 지원하지 않습니다.<br>" +
    "파일을 저장한 뒤 <b>Safari·Chrome 같은 브라우저에서 직접 열어</b> 주세요.<br>" +
    "<span>(앱 안의 첨부파일 미리보기에서는 3D가 동작하지 않습니다)</span></div>";

  /** Replace a canvas with the "no WebGL here" notice, in place. */
  function showUnavailable(canvas, options) {
    var opts = options || {};
    var msg = document.createElement("div");
    msg.className = opts.className || "r3d-unavailable";
    msg.setAttribute("role", "note");
    msg.style.cssText =
      opts.style ||
      "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;" +
        "padding:24px;text-align:center;line-height:1.6";
    msg.innerHTML = opts.html || UNAVAILABLE_HTML;
    if (canvas && canvas.parentNode) {
      canvas.parentNode.appendChild(msg);
      canvas.style.display = "none";
    }
    return msg;
  }

  return {
    create: create,
    showUnavailable: showUnavailable,
    UNAVAILABLE_HTML: UNAVAILABLE_HTML,
    hexToRgb: hexToRgb,
    perspective: perspective,
    multiply: mul
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Render3D;
