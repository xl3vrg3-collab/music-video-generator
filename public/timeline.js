/**
 * LUMN Timeline Editor — Canvas-based video timeline
 *
 * Renders a multi-track timeline on HTML5 Canvas.
 * Non-destructive: all edits are metadata (trim points, positions).
 * Clips are never re-encoded until final export.
 */

(function() {
  'use strict';

  // ─────────── Configuration ───────────
  var CONFIG = {
    TRACK_HEIGHT: 60,
    AUDIO_TRACK_HEIGHT: 36,
    RULER_HEIGHT: 28,
    TRANSITION_WIDTH: 16,
    MIN_CLIP_WIDTH: 30,
    PIXELS_PER_SECOND: 80, // Zoom level
    MIN_PPS: 20,
    MAX_PPS: 300,
    COLORS: {
      bg: '#0a0a0a',
      ruler: '#111',
      rulerText: '#666',
      rulerLine: '#333',
      playhead: '#ff3b3b',
      track: '#141414',
      clip: 'rgba(0,229,255,0.25)',
      clipBorder: '#00e5ff',
      clipText: '#e2e2e8',
      clipSelected: 'rgba(0,229,255,0.45)',
      transition: 'rgba(255,94,0,0.4)',
      transitionHandle: '#ff5e00',
      audioMusic: 'rgba(255,214,0,0.3)',
      audioVoice: 'rgba(0,229,255,0.3)',
      audioSfx: 'rgba(0,229,160,0.3)',
      trimHandle: '#ffffff',
      selection: 'rgba(106,92,255,0.15)',
    },
    FONTS: {
      ruler: '10px "JetBrains Mono", monospace',
      clip: '10px "Inter Tight", sans-serif',
      clipSmall: '8px "Inter Tight", sans-serif',
    },
  };

  // ─────────── Timeline State ───────────
  function TimelineState() {
    this.clips = [];           // [{id, sceneIndex, name, duration, trimStart, trimEnd, clipUrl, thumbUrl, color}]
    this.audioTracks = [];     // [{id, name, type, startTime, duration, volume, color}]
    this.transitions = {};     // {clipIndex: {type, duration}}
    this.textOverlays = [];    // [{text, startTime, endTime, position, style}]
    this.playheadTime = 0;
    this.selectedClipIndex = -1;
    this.hoveredClipIndex = -1;
    this.scrollX = 0;
    this.zoom = CONFIG.PIXELS_PER_SECOND;
    this.totalDuration = 0;
    this.isPlaying = false;
    this.isDragging = false;
    this.dragType = null;      // 'move', 'trimLeft', 'trimRight', 'playhead', 'scroll'
    this.dragStartX = 0;
    this.dragClipIndex = -1;
    this.dragStartValue = 0;
  }

  TimelineState.prototype.recalcDuration = function() {
    var total = 0;
    this._clipOffsets = [];  // Cache start times
    for (var i = 0; i < this.clips.length; i++) {
      var c = this.clips[i];
      this._clipOffsets.push(total);
      var effective = c.duration - (c.trimStart || 0) - (c.trimEnd || 0);
      total += Math.max(0.5, effective);
      // Add transition overlap
      var tr = this.transitions[i] || {};
      if (i > 0 && tr.duration) {
        total -= Math.min(tr.duration, effective * 0.5);
      }
    }
    this.totalDuration = total;
    return total;
  };

  TimelineState.prototype.timeToX = function(time) {
    return (time * this.zoom) - this.scrollX;
  };

  TimelineState.prototype.xToTime = function(x) {
    return (x + this.scrollX) / this.zoom;
  };

  TimelineState.prototype.getClipAtX = function(x, y) {
    var time = this.xToTime(x);
    var trackY = CONFIG.RULER_HEIGHT;

    // Check video track
    if (y >= trackY && y < trackY + CONFIG.TRACK_HEIGHT) {
      for (var i = 0; i < this.clips.length; i++) {
        var cumTime = (this._clipOffsets && this._clipOffsets[i]) || 0;
        var c = this.clips[i];
        var dur = c.duration - (c.trimStart || 0) - (c.trimEnd || 0);
        if (time >= cumTime && time < cumTime + dur) {
          // Check trim handles (8px zones at edges)
          var clipStartX = this.timeToX(cumTime);
          var clipEndX = this.timeToX(cumTime + dur);
          var handleSize = 8;

          if (x >= clipStartX && x < clipStartX + handleSize) {
            return {index: i, zone: 'trimLeft'};
          }
          if (x > clipEndX - handleSize && x <= clipEndX) {
            return {index: i, zone: 'trimRight'};
          }
          return {index: i, zone: 'body'};
        }
      }
    }

    // Check audio tracks
    var audioY = trackY + CONFIG.TRACK_HEIGHT + 8;
    for (var a = 0; a < this.audioTracks.length; a++) {
      if (y >= audioY && y < audioY + CONFIG.AUDIO_TRACK_HEIGHT) {
        var at = this.audioTracks[a];
        if (time >= at.startTime && time < at.startTime + at.duration) {
          return {index: a, zone: 'audio', trackIndex: a};
        }
      }
      audioY += CONFIG.AUDIO_TRACK_HEIGHT + 2;
    }

    return null;
  };

  // ─────────── Renderer ───────────
  function TimelineRenderer(canvas, state) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.state = state;
    this.thumbCache = {};
    this._resizeCanvas();
  }

  TimelineRenderer.prototype._resizeCanvas = function() {
    var container = this.canvas.parentElement;
    if (!container) return;
    var dpr = window.devicePixelRatio || 1;
    var w = container.clientWidth;
    var totalTracks = 1 + Math.max(1, this.state.audioTracks.length);
    var h = CONFIG.RULER_HEIGHT + CONFIG.TRACK_HEIGHT + 8 +
            (totalTracks * (CONFIG.AUDIO_TRACK_HEIGHT + 2)) + 20;
    h = Math.max(h, 160);

    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.ctx.scale(dpr, dpr);
    this.width = w;
    this.height = h;
  };

  TimelineRenderer.prototype.render = function() {
    var ctx = this.ctx;
    var s = this.state;
    var w = this.width;
    var h = this.height;

    // Clear
    ctx.fillStyle = CONFIG.COLORS.bg;
    ctx.fillRect(0, 0, w, h);

    // Ruler
    this._renderRuler();

    // Video track background
    var trackY = CONFIG.RULER_HEIGHT;
    ctx.fillStyle = CONFIG.COLORS.track;
    ctx.fillRect(0, trackY, w, CONFIG.TRACK_HEIGHT);

    // Clips
    var cumTime = 0;
    for (var i = 0; i < s.clips.length; i++) {
      var clip = s.clips[i];
      var dur = clip.duration - (clip.trimStart || 0) - (clip.trimEnd || 0);
      dur = Math.max(0.5, dur);

      var x1 = s.timeToX(cumTime);
      var x2 = s.timeToX(cumTime + dur);
      var cw = x2 - x1;

      if (x2 > 0 && x1 < w) {
        this._renderClip(clip, i, x1, trackY, cw, CONFIG.TRACK_HEIGHT);
      }

      // Transition between this clip and next
      if (i < s.clips.length - 1) {
        var tr = s.transitions[i + 1] || {};
        if (tr.type && tr.type !== 'none' && tr.duration) {
          var trX = s.timeToX(cumTime + dur - tr.duration * 0.5);
          var trW = tr.duration * s.zoom;
          this._renderTransition(tr, trX, trackY, trW, CONFIG.TRACK_HEIGHT);
        }
      }

      cumTime += dur;
      var nextTr = s.transitions[i + 1] || {};
      if (nextTr.duration) cumTime -= nextTr.duration;
    }

    // Audio tracks
    var audioY = trackY + CONFIG.TRACK_HEIGHT + 8;

    // Track label
    ctx.fillStyle = '#333';
    ctx.font = CONFIG.FONTS.clipSmall;
    ctx.fillText('VIDEO', 4, trackY + 12);

    for (var a = 0; a < s.audioTracks.length; a++) {
      var at = s.audioTracks[a];

      // Track background
      ctx.fillStyle = CONFIG.COLORS.track;
      ctx.fillRect(0, audioY, w, CONFIG.AUDIO_TRACK_HEIGHT);

      // Track label
      ctx.fillStyle = '#333';
      ctx.font = CONFIG.FONTS.clipSmall;
      ctx.fillText(at.type.toUpperCase(), 4, audioY + 12);

      // Audio bar
      var ax1 = s.timeToX(at.startTime);
      var ax2 = s.timeToX(at.startTime + at.duration);
      var colorMap = {music: CONFIG.COLORS.audioMusic, voice: CONFIG.COLORS.audioVoice, sfx: CONFIG.COLORS.audioSfx};
      ctx.fillStyle = colorMap[at.type] || CONFIG.COLORS.audioMusic;
      ctx.fillRect(Math.max(0, ax1), audioY + 2, ax2 - ax1, CONFIG.AUDIO_TRACK_HEIGHT - 4);

      // Audio name
      ctx.fillStyle = CONFIG.COLORS.clipText;
      ctx.font = CONFIG.FONTS.clipSmall;
      ctx.fillText(at.name || at.type, Math.max(4, ax1 + 4), audioY + CONFIG.AUDIO_TRACK_HEIGHT / 2 + 3);

      audioY += CONFIG.AUDIO_TRACK_HEIGHT + 2;
    }

    // Text overlay indicators
    for (var t = 0; t < s.textOverlays.length; t++) {
      var ovl = s.textOverlays[t];
      var ox1 = s.timeToX(ovl.startTime);
      var ox2 = s.timeToX(ovl.endTime);
      ctx.fillStyle = 'rgba(106,92,255,0.2)';
      ctx.fillRect(ox1, trackY - 4, ox2 - ox1, 4);
    }

    // Playhead
    var phX = s.timeToX(s.playheadTime);
    if (phX >= 0 && phX <= w) {
      ctx.strokeStyle = CONFIG.COLORS.playhead;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(phX, 0);
      ctx.lineTo(phX, h);
      ctx.stroke();

      // Playhead handle
      ctx.fillStyle = CONFIG.COLORS.playhead;
      ctx.beginPath();
      ctx.moveTo(phX - 6, 0);
      ctx.lineTo(phX + 6, 0);
      ctx.lineTo(phX, 10);
      ctx.closePath();
      ctx.fill();

      // Time label
      ctx.fillStyle = '#fff';
      ctx.font = CONFIG.FONTS.ruler;
      var phMins = Math.floor(s.playheadTime / 60);
      var phSecs = (s.playheadTime % 60).toFixed(1);
      ctx.fillText(phMins + ':' + (phSecs < 10 ? '0' : '') + phSecs, phX + 4, 22);
    }
  };

  TimelineRenderer.prototype._renderRuler = function() {
    var ctx = this.ctx;
    var s = this.state;
    var w = this.width;

    ctx.fillStyle = CONFIG.COLORS.ruler;
    ctx.fillRect(0, 0, w, CONFIG.RULER_HEIGHT);

    // Time marks
    var step = 1; // 1 second
    if (s.zoom < 40) step = 5;
    if (s.zoom < 20) step = 10;
    if (s.zoom > 150) step = 0.5;

    var startTime = Math.floor(s.xToTime(0) / step) * step;
    var endTime = s.xToTime(w);

    ctx.strokeStyle = CONFIG.COLORS.rulerLine;
    ctx.fillStyle = CONFIG.COLORS.rulerText;
    ctx.font = CONFIG.FONTS.ruler;
    ctx.lineWidth = 1;

    for (var t = startTime; t <= endTime; t += step) {
      var x = s.timeToX(t);
      if (x < 0) continue;

      var isWhole = Math.abs(t - Math.round(t)) < 0.01;

      ctx.beginPath();
      ctx.moveTo(x, isWhole ? 14 : 20);
      ctx.lineTo(x, CONFIG.RULER_HEIGHT);
      ctx.stroke();

      if (isWhole) {
        var mins = Math.floor(t / 60);
        var secs = Math.floor(t % 60);
        ctx.fillText(mins + ':' + (secs < 10 ? '0' : '') + secs, x + 2, 12);
      }
    }
  };

  TimelineRenderer.prototype._renderClip = function(clip, index, x, y, w, h) {
    var ctx = this.ctx;
    var s = this.state;
    var isSelected = index === s.selectedClipIndex;
    var isHovered = index === s.hoveredClipIndex;

    // Clip body
    ctx.fillStyle = isSelected ? CONFIG.COLORS.clipSelected : CONFIG.COLORS.clip;
    ctx.fillRect(x, y + 2, w, h - 4);

    // Border
    ctx.strokeStyle = isSelected ? '#fff' : (isHovered ? CONFIG.COLORS.clipBorder : 'rgba(0,229,255,0.3)');
    ctx.lineWidth = isSelected ? 2 : 1;
    ctx.strokeRect(x, y + 2, w, h - 4);

    // Clip name
    if (w > 40) {
      ctx.fillStyle = CONFIG.COLORS.clipText;
      ctx.font = CONFIG.FONTS.clip;
      var label = clip.name || ('Scene ' + (clip.sceneIndex + 1));
      ctx.fillText(label, x + 6, y + 18, w - 12);

      // Duration
      ctx.font = CONFIG.FONTS.clipSmall;
      ctx.fillStyle = '#888';
      var dur = clip.duration - (clip.trimStart || 0) - (clip.trimEnd || 0);
      ctx.fillText(dur.toFixed(1) + 's', x + 6, y + h - 10, w - 12);
    }

    // Scene number badge
    ctx.fillStyle = CONFIG.COLORS.clipBorder;
    ctx.fillRect(x, y + 2, 3, h - 4);

    // Trim handles (visible on hover/select)
    if (isSelected || isHovered) {
      // Left handle
      ctx.fillStyle = CONFIG.COLORS.trimHandle;
      ctx.fillRect(x, y + h/2 - 8, 4, 16);
      // Right handle
      ctx.fillRect(x + w - 4, y + h/2 - 8, 4, 16);
    }

    // Thumbnail placeholder (colored stripe based on shot type)
    if (clip.thumbUrl && w > 60) {
      // We'll load thumbnails asynchronously
      var thumb = this.thumbCache[clip.thumbUrl];
      if (thumb && thumb.complete) {
        ctx.globalAlpha = 0.3;
        ctx.drawImage(thumb, x + 4, y + 4, Math.min(w - 8, 80), h - 8);
        ctx.globalAlpha = 1.0;
      } else if (!thumb) {
        var img = new Image();
        img.src = clip.thumbUrl;
        this.thumbCache[clip.thumbUrl] = img;
        var self = this;
        img.onload = function() { self.render(); };
      }
    }
  };

  TimelineRenderer.prototype._renderTransition = function(tr, x, y, w, h) {
    var ctx = this.ctx;

    // Transition overlay
    ctx.fillStyle = CONFIG.COLORS.transition;
    ctx.fillRect(x, y + 2, w, h - 4);

    // Handle diamond
    var cx = x + w / 2;
    var cy = y + h / 2;
    ctx.fillStyle = CONFIG.COLORS.transitionHandle;
    ctx.beginPath();
    ctx.moveTo(cx, cy - 6);
    ctx.lineTo(cx + 6, cy);
    ctx.lineTo(cx, cy + 6);
    ctx.lineTo(cx - 6, cy);
    ctx.closePath();
    ctx.fill();

    // Label
    if (w > 30) {
      ctx.fillStyle = '#fff';
      ctx.font = CONFIG.FONTS.clipSmall;
      ctx.fillText(tr.type || '', x + 2, y + h - 8, w - 4);
    }
  };

  // ─────────── Interaction Controller ───────────
  function TimelineController(canvas, state, renderer) {
    this.canvas = canvas;
    this.state = state;
    this.renderer = renderer;
    this.onUpdate = null; // Callback when state changes
    this._bindEvents();
  }

  TimelineController.prototype._bindEvents = function() {
    var self = this;
    var canvas = this.canvas;

    canvas.addEventListener('mousedown', function(e) { self._onMouseDown(e); });
    canvas.addEventListener('mousemove', function(e) { self._onMouseMove(e); });
    canvas.addEventListener('mouseup', function(e) { self._onMouseUp(e); });
    canvas.addEventListener('mouseleave', function(e) { self._onMouseUp(e); });
    canvas.addEventListener('wheel', function(e) { self._onWheel(e); });
    canvas.addEventListener('dblclick', function(e) { self._onDblClick(e); });

    // Touch support
    canvas.addEventListener('touchstart', function(e) {
      e.preventDefault();
      var touch = e.touches[0];
      var rect = canvas.getBoundingClientRect();
      self._onMouseDown({clientX: touch.clientX, clientY: touch.clientY, target: canvas, offsetX: touch.clientX - rect.left, offsetY: touch.clientY - rect.top, preventDefault: function(){}});
    });
    canvas.addEventListener('touchmove', function(e) {
      e.preventDefault();
      var touch = e.touches[0];
      var rect = canvas.getBoundingClientRect();
      self._onMouseMove({clientX: touch.clientX, clientY: touch.clientY, target: canvas, offsetX: touch.clientX - rect.left, offsetY: touch.clientY - rect.top});
    });
    canvas.addEventListener('touchend', function(e) { self._onMouseUp(e); });
  };

  TimelineController.prototype._getPos = function(e) {
    var rect = this.canvas.getBoundingClientRect();
    return {x: e.clientX - rect.left, y: e.clientY - rect.top};
  };

  TimelineController.prototype._onMouseDown = function(e) {
    var pos = this._getPos(e);
    var s = this.state;

    // Ruler click = move playhead
    if (pos.y < CONFIG.RULER_HEIGHT) {
      s.dragType = 'playhead';
      s.isDragging = true;
      s.playheadTime = Math.max(0, s.xToTime(pos.x));
      this.renderer.render();
      this._emitUpdate();
      return;
    }

    // Check clip hit
    var hit = s.getClipAtX(pos.x, pos.y);
    if (hit) {
      s.selectedClipIndex = hit.index;
      s.isDragging = true;
      s.dragClipIndex = hit.index;
      s.dragStartX = pos.x;

      if (hit.zone === 'trimLeft') {
        s.dragType = 'trimLeft';
        s.dragStartValue = s.clips[hit.index].trimStart || 0;
      } else if (hit.zone === 'trimRight') {
        s.dragType = 'trimRight';
        s.dragStartValue = s.clips[hit.index].trimEnd || 0;
      } else if (hit.zone === 'body') {
        s.dragType = 'move';
      } else if (hit.zone === 'audio') {
        s.dragType = 'moveAudio';
        s.dragClipIndex = hit.trackIndex;
        s.dragStartValue = s.audioTracks[hit.trackIndex].startTime;
      }

      this.renderer.render();
      this._emitUpdate();
    } else {
      s.selectedClipIndex = -1;
      // Scroll drag on empty area
      s.dragType = 'scroll';
      s.isDragging = true;
      s.dragStartX = pos.x;
      s.dragStartValue = s.scrollX;
      this.renderer.render();
    }
  };

  TimelineController.prototype._onMouseMove = function(e) {
    if (this._rafPending) return;
    this._rafPending = true;
    var self = this;
    requestAnimationFrame(function() {
      self._rafPending = false;
      self._doMouseMove(e);
    });
  };

  TimelineController.prototype._doMouseMove = function(e) {
    var pos = this._getPos(e);
    var s = this.state;

    if (s.isDragging) {
      var dx = pos.x - s.dragStartX;
      var dt = dx / s.zoom;

      if (s.dragType === 'playhead') {
        s.playheadTime = Math.max(0, Math.min(s.totalDuration, s.xToTime(pos.x)));
      } else if (s.dragType === 'trimLeft') {
        var clip = s.clips[s.dragClipIndex];
        if (clip) {
          clip.trimStart = Math.max(0, Math.min(clip.duration * 0.8, s.dragStartValue + dt));
          s.recalcDuration();
        }
      } else if (s.dragType === 'trimRight') {
        var clip = s.clips[s.dragClipIndex];
        if (clip) {
          clip.trimEnd = Math.max(0, Math.min(clip.duration * 0.8, s.dragStartValue - dt));
          s.recalcDuration();
        }
      } else if (s.dragType === 'move') {
        // Reorder by dragging past midpoint of adjacent clip
        var fromIdx = s.dragClipIndex;
        var toIdx = fromIdx;

        // Calculate which position this would snap to (using cached offsets)
        for (var i = 0; i < s.clips.length; i++) {
          var cumTime = (s._clipOffsets && s._clipOffsets[i]) || 0;
          var dur = s.clips[i].duration - (s.clips[i].trimStart || 0) - (s.clips[i].trimEnd || 0);
          var midX = s.timeToX(cumTime + dur / 2);
          if (pos.x < midX) {
            toIdx = i;
            break;
          }
          toIdx = i + 1;
        }

        if (toIdx !== fromIdx && toIdx !== fromIdx + 1) {
          var clip = s.clips.splice(fromIdx, 1)[0];
          if (toIdx > fromIdx) toIdx--;
          s.clips.splice(toIdx, 0, clip);
          s.dragClipIndex = toIdx;
          s.selectedClipIndex = toIdx;
          s.recalcDuration();
        }
      } else if (s.dragType === 'moveAudio') {
        var track = s.audioTracks[s.dragClipIndex];
        if (track) {
          track.startTime = Math.max(0, s.dragStartValue + dt);
        }
      } else if (s.dragType === 'scroll') {
        s.scrollX = Math.max(0, s.dragStartValue - dx);
      }

      this.renderer.render();
      return;
    }

    // Hover detection
    var hit = s.getClipAtX(pos.x, pos.y);
    var oldHover = s.hoveredClipIndex;
    s.hoveredClipIndex = hit ? hit.index : -1;

    // Cursor
    if (hit) {
      if (hit.zone === 'trimLeft' || hit.zone === 'trimRight') {
        this.canvas.style.cursor = 'col-resize';
      } else if (hit.zone === 'body' || hit.zone === 'audio') {
        this.canvas.style.cursor = 'grab';
      }
    } else if (pos.y < CONFIG.RULER_HEIGHT) {
      this.canvas.style.cursor = 'text';
    } else {
      this.canvas.style.cursor = 'default';
    }

    if (oldHover !== s.hoveredClipIndex) {
      this.renderer.render();
    }
  };

  TimelineController.prototype._onMouseUp = function(e) {
    var s = this.state;
    if (s.isDragging) {
      s.isDragging = false;
      s.dragType = null;
      this.canvas.style.cursor = 'default';
      this.renderer.render();
      this._emitUpdate();
    }
  };

  TimelineController.prototype._onWheel = function(e) {
    e.preventDefault();
    var s = this.state;

    if (e.ctrlKey || e.metaKey) {
      // Zoom
      var pos = this._getPos(e);
      var timeAtCursor = s.xToTime(pos.x);

      var zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
      s.zoom = Math.max(CONFIG.MIN_PPS, Math.min(CONFIG.MAX_PPS, s.zoom * zoomFactor));

      // Keep cursor position stable
      s.scrollX = (timeAtCursor * s.zoom) - pos.x;
      s.scrollX = Math.max(0, s.scrollX);
    } else {
      // Scroll
      s.scrollX = Math.max(0, s.scrollX + e.deltaX + e.deltaY);
    }

    this.renderer.render();
  };

  TimelineController.prototype._onDblClick = function(e) {
    var pos = this._getPos(e);
    var s = this.state;
    var hit = s.getClipAtX(pos.x, pos.y);

    if (hit && hit.zone === 'body') {
      // Double-click clip = scroll to it in Shots workspace
      if (window._scrollToScene) {
        window._scrollToScene(s.clips[hit.index].sceneIndex);
      }
    }
  };

  TimelineController.prototype._emitUpdate = function() {
    if (this.onUpdate) this.onUpdate(this.state);
  };

  // ─────────── Public API ───────────
  function LumnTimeline(containerId) {
    this.container = document.getElementById(containerId);
    if (!this.container) return;

    // Create canvas
    this.canvas = document.createElement('canvas');
    this.canvas.style.cssText = 'width:100%;border-radius:4px;cursor:default;';
    this.container.innerHTML = '';
    this.container.appendChild(this.canvas);

    this.state = new TimelineState();
    this.renderer = new TimelineRenderer(this.canvas, this.state);
    this.controller = new TimelineController(this.canvas, this.state, this.renderer);

    // Resize observer
    var self = this;
    if (window.ResizeObserver) {
      new ResizeObserver(function() {
        self.renderer._resizeCanvas();
        self.renderer.render();
      }).observe(this.container);
    }

    // Sync callback
    this.controller.onUpdate = function(state) {
      self._syncToApp(state);
    };
  }

  LumnTimeline.prototype.loadFromScenes = function(scenes, audioTracks, transitions, textOverlays) {
    var s = this.state;
    s.clips = [];

    (scenes || []).forEach(function(sc, i) {
      s.clips.push({
        id: sc.id || ('scene_' + i),
        sceneIndex: i,
        name: sc.summary || sc.description || ('Scene ' + (i + 1)),
        duration: parseFloat(sc.duration) || 4,
        trimStart: sc.trimStart || 0,
        trimEnd: sc.trimEnd || 0,
        clipUrl: sc.clip_url || sc.clipUrl || '',
        thumbUrl: sc.first_frame_url || sc.firstFrameUrl || '',
        shotType: sc.shot_type || 'medium',
      });
    });

    s.audioTracks = (audioTracks || []).map(function(t) {
      return {
        id: t.id || ('audio_' + Math.random().toString(36).substr(2, 6)),
        name: t.name || t.type,
        type: t.type || 'music',
        startTime: t.startTime || 0,
        duration: t.duration || 10,
        volume: t.volume || 80,
      };
    });

    s.transitions = transitions || {};
    s.textOverlays = textOverlays || [];
    s.recalcDuration();
    this.renderer._resizeCanvas();
    this.renderer.render();
  };

  LumnTimeline.prototype._syncToApp = function(state) {
    // Sync timeline state back to app
    var scenes = window._autoScenes || window._scenes || [];

    // Update clip order
    state.clips.forEach(function(clip, i) {
      var sceneIdx = clip.sceneIndex;
      if (scenes[sceneIdx]) {
        scenes[sceneIdx].trimStart = clip.trimStart;
        scenes[sceneIdx].trimEnd = clip.trimEnd;
      }
    });

    // Reorder scenes if clips were moved
    var newOrder = state.clips.map(function(c) { return c.sceneIndex; });
    var reordered = false;
    for (var i = 0; i < newOrder.length - 1; i++) {
      if (newOrder[i] > newOrder[i + 1]) { reordered = true; break; }
    }

    if (reordered && scenes.length === state.clips.length) {
      var newScenes = newOrder.map(function(idx) { return scenes[idx]; });
      if (window._autoScenes) {
        window._autoScenes = newScenes;
      } else {
        window._scenes = newScenes;
      }
      // Update clip scene indices
      state.clips.forEach(function(c, i) { c.sceneIndex = i; });
    }

    // Sync transitions
    window._sceneTransitions = state.transitions;

    // Sync audio tracks
    window._audioTracks = state.audioTracks;

    // Sync text overlays
    window._textOverlays = state.textOverlays;

    // Update duration display
    var durEl = document.getElementById('editTotalDuration');
    if (durEl) {
      var total = state.totalDuration;
      var mins = Math.floor(total / 60);
      var secs = Math.floor(total % 60);
      durEl.textContent = mins + ':' + (secs < 10 ? '0' : '') + secs;
    }

    window._hasUnsavedChanges = true;
  };

  LumnTimeline.prototype.setPlayhead = function(time) {
    this.state.playheadTime = time;
    this.renderer.render();
  };

  LumnTimeline.prototype.zoomIn = function() {
    this.state.zoom = Math.min(CONFIG.MAX_PPS, this.state.zoom * 1.2);
    this.renderer.render();
  };

  LumnTimeline.prototype.zoomOut = function() {
    this.state.zoom = Math.max(CONFIG.MIN_PPS, this.state.zoom * 0.8);
    this.renderer.render();
  };

  LumnTimeline.prototype.zoomToFit = function() {
    if (this.state.totalDuration > 0) {
      this.state.zoom = (this.renderer.width - 40) / this.state.totalDuration;
      this.state.scrollX = 0;
      this.renderer.render();
    }
  };

  LumnTimeline.prototype.getState = function() {
    return this.state;
  };

  // Export
  window.LumnTimeline = LumnTimeline;

})();
