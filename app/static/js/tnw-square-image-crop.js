/**
 * Square crop UI for image uploads (default client output 300×300 PNG).
 * Depends on Bootstrap 5 Modal and window.showTnwMessage (optional).
 */
(function (window, document) {
  "use strict";

  var DEFAULT_OUT = 300;
  var MIN_SIDE_DISP = 40;

  var els = {};
  var modalInstance = null;
  var objUrl = null;
  var dragging = null;
  var fx = 0;
  var fy = 0;
  var side = 0;
  var dispW = 0;
  var dispH = 0;
  var natW = 0;
  var natH = 0;
  var pendingOpts = null;
  var appliedThisOpen = false;
  var layoutRetryCount = 0;
  var LAYOUT_RETRY_MAX = 90;
  var currentOut = DEFAULT_OUT;

  function msgTitle(opts) {
    return (opts && opts.messageTitle) || "Upload";
  }

  function isResizeHandleEl(t) {
    if (!t || !els.handle) return false;
    return t === els.handle || (typeof t.closest === "function" && t.closest(".tnw-square-crop-handle, .ew-crop-handle"));
  }

  function clampFrame() {
    var maxS = Math.min(dispW, dispH);
    if (side > maxS) side = maxS;
    if (side < MIN_SIDE_DISP) side = Math.min(MIN_SIDE_DISP, maxS);
    if (fx < 0) fx = 0;
    if (fy < 0) fy = 0;
    if (fx + side > dispW) fx = dispW - side;
    if (fy + side > dispH) fy = dispH - side;
    if (fx < 0) fx = 0;
    if (fy < 0) fy = 0;
  }

  function paintFrame() {
    els.frame.style.left = fx + "px";
    els.frame.style.top = fy + "px";
    els.frame.style.width = side + "px";
    els.frame.style.height = side + "px";
  }

  function layoutFromImage() {
    natW = els.img.naturalWidth;
    natH = els.img.naturalHeight;
    dispW = els.img.clientWidth;
    dispH = els.img.clientHeight;
    if (!dispW || !dispH || !natW || !natH) {
      layoutRetryCount++;
      if (layoutRetryCount < LAYOUT_RETRY_MAX && objUrl) {
        requestAnimationFrame(layoutFromImage);
      }
      return;
    }
    layoutRetryCount = 0;
    var maxS = Math.min(dispW, dispH);
    side = Math.round(maxS * 0.72);
    if (side < MIN_SIDE_DISP) side = Math.min(MIN_SIDE_DISP, maxS);
    fx = (dispW - side) / 2;
    fy = (dispH - side) / 2;
    clampFrame();
    paintFrame();
  }

  function revoke() {
    if (objUrl) {
      URL.revokeObjectURL(objUrl);
      objUrl = null;
    }
    els.img.removeAttribute("src");
  }

  function onPointerMove(e) {
    if (!dragging) return;
    if (dragging.type === "move") {
      fx = dragging.origFx + (e.clientX - dragging.startX);
      fy = dragging.origFy + (e.clientY - dragging.startY);
      clampFrame();
      paintFrame();
    } else if (dragging.type === "resize") {
      var delta = e.clientX - dragging.startX;
      side = dragging.origSide + delta;
      if (side < MIN_SIDE_DISP) side = MIN_SIDE_DISP;
      var maxByPos = Math.min(dispW - dragging.anchorFx, dispH - dragging.anchorFy);
      var maxS = Math.min(maxByPos, Math.min(dispW, dispH));
      if (side > maxS) side = maxS;
      fx = dragging.anchorFx;
      fy = dragging.anchorFy;
      clampFrame();
      paintFrame();
    }
  }

  function onPointerUp(e) {
    if (e && typeof e.pointerId === "number") {
      try {
        if (els.frame && els.frame.releasePointerCapture && els.frame.hasPointerCapture(e.pointerId)) {
          els.frame.releasePointerCapture(e.pointerId);
        }
      } catch (_a) {}
      try {
        if (els.handle && els.handle.releasePointerCapture && els.handle.hasPointerCapture(e.pointerId)) {
          els.handle.releasePointerCapture(e.pointerId);
        }
      } catch (_b) {}
    }
    dragging = null;
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", onPointerUp);
    document.removeEventListener("pointercancel", onPointerUp);
    document.removeEventListener("mousemove", onPointerMove);
    document.removeEventListener("mouseup", onPointerUp);
  }

  function tryCapturePointer(el, e) {
    if (!el || e.pointerId == null || typeof el.setPointerCapture !== "function") return;
    try {
      el.setPointerCapture(e.pointerId);
    } catch (_c) {}
  }

  function onFramePointerDown(e) {
    if (isResizeHandleEl(e.target)) return;
    e.preventDefault();
    tryCapturePointer(els.frame, e);
    dragging = {
      type: "move",
      startX: e.clientX,
      startY: e.clientY,
      origFx: fx,
      origFy: fy,
    };
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerUp);
  }

  function onHandlePointerDown(e) {
    e.preventDefault();
    e.stopPropagation();
    tryCapturePointer(els.handle, e);
    dragging = {
      type: "resize",
      startX: e.clientX,
      startY: e.clientY,
      origSide: side,
      anchorFx: fx,
      anchorFy: fy,
    };
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerUp);
  }

  function onFrameMouseDown(e) {
    if (e.button !== 0) return;
    if (isResizeHandleEl(e.target)) return;
    e.preventDefault();
    dragging = {
      type: "move",
      startX: e.clientX,
      startY: e.clientY,
      origFx: fx,
      origFy: fy,
    };
    document.addEventListener("mousemove", onPointerMove);
    document.addEventListener("mouseup", onPointerUp);
  }

  function onHandleMouseDown(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    dragging = {
      type: "resize",
      startX: e.clientX,
      startY: e.clientY,
      origSide: side,
      anchorFx: fx,
      anchorFy: fy,
    };
    document.addEventListener("mousemove", onPointerMove);
    document.addEventListener("mouseup", onPointerUp);
  }

  function computeCrop() {
    var scaleX = natW / dispW;
    var scaleY = natH / dispH;
    var sx = Math.round(fx * scaleX);
    var sy = Math.round(fy * scaleY);
    var sw = Math.round(side * scaleX);
    var sh = sw;
    sx = Math.max(0, Math.min(sx, natW - 1));
    sy = Math.max(0, Math.min(sy, natH - 1));
    sw = Math.min(sw, natW - sx, natH - sy);
    sh = sw;
    return { sx: sx, sy: sy, sw: sw, sh: sh };
  }

  function cropFileName(original, baseName) {
    var base = baseName || "image";
    if (original && String(original).trim()) {
      base = String(original).replace(/\.[^.\\/]+$/, "");
      if (!base) base = baseName || "image";
    }
    return base + ".png";
  }

  function updateSizeHint(outSize) {
    if (!els.sizeHint) return;
    els.sizeHint.textContent =
      "The saved image is always a " + outSize + "×" + outSize + " pixel square.";
  }

  function onApplyClick() {
    var opts = pendingOpts;
    var outSize = (opts && opts.outSize) || currentOut || DEFAULT_OUT;
    var c = computeCrop();
    if (!c.sw || !c.sh) {
      if (window.showTnwMessage) {
        window.showTnwMessage("Could not read that image for cropping.", {
          title: msgTitle(opts),
          variant: "warning",
        });
      }
      return;
    }
    var canvas = document.createElement("canvas");
    canvas.width = outSize;
    canvas.height = outSize;
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    try {
      ctx.drawImage(els.img, c.sx, c.sy, c.sw, c.sh, 0, 0, outSize, outSize);
    } catch (_e) {
      if (window.showTnwMessage) {
        window.showTnwMessage("Could not crop that image.", { title: msgTitle(opts), variant: "warning" });
      }
      return;
    }
    canvas.toBlob(
      function (blob) {
        if (!blob) {
          if (window.showTnwMessage) {
            window.showTnwMessage("Could not build the cropped image.", {
              title: msgTitle(opts),
              variant: "warning",
            });
          }
          return;
        }
        appliedThisOpen = true;
        var name = cropFileName(opts && opts.originalName, opts && opts.defaultFileNameBase);
        var file = new File([blob], name, { type: "image/png" });
        if (opts && typeof opts.onCroppedFile === "function") {
          opts.onCroppedFile(file);
        }
        if (modalInstance) modalInstance.hide();
      },
      "image/png",
      1
    );
  }

  function scheduleLayout() {
    requestAnimationFrame(function () {
      layoutFromImage();
      requestAnimationFrame(layoutFromImage);
    });
  }

  function onModalHidden() {
    if (!appliedThisOpen && pendingOpts && pendingOpts.clearInputOnCancel && pendingOpts.fileInput) {
      try {
        pendingOpts.fileInput.value = "";
      } catch (_e) {}
    }
    appliedThisOpen = false;
    pendingOpts = null;
    dragging = null;
    layoutRetryCount = 0;
    revoke();
  }

  function open(file, opts) {
    opts = opts || {};
    if (!file || !file.type || file.type.indexOf("image") !== 0) {
      if (window.showTnwMessage) {
        window.showTnwMessage("Please choose an image file.", { title: msgTitle(opts), variant: "warning" });
      }
      return;
    }
    if (!modalInstance || !els.img) return;
    currentOut = opts.outSize || DEFAULT_OUT;
    if (els.titleEl && opts.dialogTitle) {
      els.titleEl.textContent = opts.dialogTitle;
    }
    updateSizeHint(currentOut);
    pendingOpts = opts;
    appliedThisOpen = false;
    layoutRetryCount = 0;
    revoke();
    objUrl = URL.createObjectURL(file);
    els.img.onload = function () {
      scheduleLayout();
    };
    els.img.src = objUrl;
    modalInstance.show();
  }

  function install(cfg) {
    els.modal = cfg.modal;
    els.viewport = cfg.viewport;
    els.img = cfg.img;
    els.frame = cfg.frame;
    els.handle = cfg.handle;
    els.titleEl = cfg.titleEl || null;
    els.sizeHint = cfg.sizeHint || null;
    if (cfg.btnApply) {
      cfg.btnApply.addEventListener("click", onApplyClick);
    }
    if (els.img) {
      els.img.draggable = false;
      els.img.addEventListener("dragstart", function (ev) {
        ev.preventDefault();
      });
    }
    if (typeof window.bootstrap !== "undefined" && window.bootstrap.Modal) {
      modalInstance = window.bootstrap.Modal.getOrCreateInstance(els.modal);
    }
    if (window.PointerEvent) {
      els.frame.addEventListener("pointerdown", onFramePointerDown);
      els.handle.addEventListener("pointerdown", onHandlePointerDown);
    } else {
      els.frame.addEventListener("mousedown", onFrameMouseDown);
      els.handle.addEventListener("mousedown", onHandleMouseDown);
    }
    els.modal.addEventListener("shown.bs.modal", scheduleLayout);
    els.modal.addEventListener("hidden.bs.modal", onModalHidden);
    window.__tnwSquareCropInstalled = true;
  }

  var api = {
    DEFAULT_OUT: DEFAULT_OUT,
    install: install,
    open: open,
  };

  window.tnwSquareImageCrop = api;
  window.tnwEwGroupImageCrop = api;
})(window, document);
