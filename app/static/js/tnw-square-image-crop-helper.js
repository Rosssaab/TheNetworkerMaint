/**
 * Helpers to wire file inputs to tnwSquareImageCrop (assign cropped file + preview hooks).
 */
(function (window, document) {
  "use strict";

  function assignFileToInput(fileInput, file) {
    if (!fileInput || !file) return false;
    try {
      var dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      return true;
    } catch (_e) {
      return false;
    }
  }

  function installCropModalIfPresent() {
    if (window.__tnwSquareCropInstalled || !window.tnwSquareImageCrop) return;
    var modal = document.getElementById("tnwSquareImageCropModal");
    if (!modal) return;
    window.tnwSquareImageCrop.install({
      modal: modal,
      viewport: document.getElementById("tnwSquareCropViewport"),
      img: document.getElementById("tnwSquareCropImg"),
      frame: document.getElementById("tnwSquareCropFrame"),
      handle: document.getElementById("tnwSquareCropResizeHandle"),
      btnApply: document.getElementById("tnwSquareImageCropApply"),
      titleEl: document.getElementById("tnwSquareImageCropTitle"),
      sizeHint: document.getElementById("tnwSquareCropSizeHint"),
    });
  }

  function openSquareImageCrop(file, opts) {
    opts = opts || {};
    installCropModalIfPresent();
    if (!window.tnwSquareImageCrop || typeof window.tnwSquareImageCrop.open !== "function") {
      if (typeof opts.onCroppedFile === "function") {
        opts.onCroppedFile(file);
      }
      return false;
    }
    window.tnwSquareImageCrop.open(file, opts);
    return true;
  }

  function openSquareImageCropFromInput(fileInput, file, options) {
    options = options || {};
    var fromPicker = options.fromPicker !== false;
    openSquareImageCrop(file, {
      clearInputOnCancel: fromPicker,
      fileInput: fileInput,
      originalName: (file && file.name) || "",
      dialogTitle: options.dialogTitle || "Crop image",
      messageTitle: options.messageTitle || "Upload",
      outSize: options.outSize || window.tnwSquareImageCrop.DEFAULT_OUT,
      defaultFileNameBase: options.defaultFileNameBase || "image",
      onCroppedFile: function (cropped) {
        if (fileInput) assignFileToInput(fileInput, cropped);
        if (typeof options.onCroppedFile === "function") {
          options.onCroppedFile(cropped);
        }
      },
    });
    return true;
  }

  function wireFileInputToSquareCrop(fileInput, options) {
    if (!fileInput) return;
    fileInput.addEventListener("change", function () {
      var f = fileInput.files && fileInput.files[0];
      if (!f) return;
      openSquareImageCropFromInput(fileInput, f, options || {});
    });
  }

  window.tnwAssignFileToInput = assignFileToInput;
  window.tnwOpenSquareImageCrop = openSquareImageCrop;
  window.tnwOpenSquareImageCropFromInput = openSquareImageCropFromInput;
  window.tnwWireFileInputToSquareCrop = wireFileInputToSquareCrop;

  function boot() {
    installCropModalIfPresent();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(window, document);
