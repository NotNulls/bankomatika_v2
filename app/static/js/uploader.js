/**
 * Bank Statement Converter — select/inspect/convert flow, drag/drop uploader,
 * progress display and the inline editable/manual transaction grid.
 */
(function () {
  "use strict";

  var root = document.getElementById("converter-app");
  if (!root) return;

  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("file-input");
  var selectedPanel = document.getElementById("selected-file-panel");
  var selectedFilename = document.getElementById("selected-filename");
  var selectedFilesize = document.getElementById("selected-filesize");
  var convertButton = document.getElementById("convert-selected");
  var inspectButton = document.getElementById("inspect-selected");
  var removeButton = document.getElementById("remove-selected");
  var progressWrap = document.getElementById("progress-wrap");
  var progressBar = document.getElementById("progress-bar");
  var progressPercent = document.getElementById("progress-percent");
  var progressLabel = document.getElementById("progress-label");
  var progressTrack = progressWrap ? progressWrap.querySelector('[role="progressbar"]') : null;
  var uploadAlert = document.getElementById("upload-alert");
  var manualOffer = document.getElementById("manual-offer");
  var openManualButton = document.getElementById("open-manual-editor");
  var resultSection = document.getElementById("result-section");
  var manualPreview = document.getElementById("manual-preview");
  var manualPdfFrame = document.getElementById("manual-pdf-frame");
  var resultTitle = document.getElementById("result-title");
  var resultToolbarTitle = document.getElementById("result-toolbar-title");
  var resultToolbarCopy = document.getElementById("result-toolbar-copy");
  var resultFilename = document.getElementById("result-filename");
  var resultBank = document.getElementById("result-bank");
  var resultPages = document.getElementById("result-pages");
  var resultRows = document.getElementById("result-rows");
  var resultQuota = document.getElementById("result-quota");
  var transactionsBody = document.getElementById("transactions-body");
  var addRowButton = document.getElementById("add-row");
  var downloadStatus = document.getElementById("download-status");
  var downloadButtons = Array.prototype.slice.call(
    document.querySelectorAll("[data-export]")
  );

  if (!dropzone || !fileInput || !transactionsBody) return;

  var messages = {
    invalid: root.dataset.msgInvalid || "Please choose a PDF bank statement.",
    large: root.dataset.msgLarge || "That PDF is larger than the upload limit.",
    uploading: root.dataset.msgUploading || "Uploading and extracting transactions…",
    parsing: root.dataset.msgParsing || "Extracting transactions…",
    done: root.dataset.msgDone || "Conversion complete.",
    error: root.dataset.msgError || "The conversion could not be completed.",
    quota: root.dataset.msgQuota || "Your free page limit has been reached.",
    empty: root.dataset.msgEmpty || "No transactions were found in this PDF.",
    downloading: root.dataset.msgDownloading || "Preparing your download…",
    downloadError: root.dataset.msgDownloadError || "The download could not be prepared.",
    expired: root.dataset.msgExpired || "Your preview expired. Please upload the PDF again.",
    fileLabel: root.dataset.msgFileLabel || "Selected file",
    rowsLabel: root.dataset.msgRowsLabel || "transactions found",
    pageLabel: root.dataset.msgPageLabel || "page",
    pagesLabel: root.dataset.msgPagesLabel || "pages",
    manualReady: root.dataset.msgManualReady || "Manual extraction is ready.",
    manualLabel: root.dataset.msgManualLabel || "Manual extraction",
    inspect: root.dataset.msgInspect || "The PDF opened in a new tab for inspection."
  };

  var maxBytes = (parseInt(root.dataset.maxMb, 10) || 25) * 1024 * 1024;
  var uploadUrl = root.dataset.uploadUrl;
  var registerUrl = root.dataset.registerUrl || "/auth/register";
  var pricingUrl = root.dataset.pricingUrl || "/pricing";
  var currentResponse = null;
  var manualContext = null;
  var selectedFile = null;
  var selectedObjectUrl = null;
  var busy = false;

  function setAlert(message, kind, action) {
    if (!uploadAlert) return;
    uploadAlert.textContent = "";
    uploadAlert.classList.remove("d-none", "alert-error", "alert-info", "alert-success");
    uploadAlert.classList.add("alert-" + (kind || "error"));
    var text = document.createElement("span");
    text.textContent = message;
    uploadAlert.appendChild(text);
    if (action && action.href && action.label) {
      var link = document.createElement("a");
      link.href = action.href;
      link.textContent = " " + action.label;
      link.className = "ms-1";
      uploadAlert.appendChild(link);
    }
  }

  function clearAlert() {
    if (!uploadAlert) return;
    uploadAlert.classList.add("d-none");
    uploadAlert.textContent = "";
  }

  function setProgress(percent) {
    var value = Math.max(0, Math.min(100, Math.round(percent)));
    if (progressBar) progressBar.style.width = value + "%";
    if (progressPercent) progressPercent.textContent = value + "%";
    if (progressTrack) progressTrack.setAttribute("aria-valuenow", String(value));
  }

  function showProgress(label, percent) {
    if (!progressWrap) return;
    progressWrap.classList.remove("d-none");
    if (progressLabel) progressLabel.textContent = label || messages.uploading;
    setProgress(percent || 0);
  }

  function hideProgress() {
    if (progressWrap) progressWrap.classList.add("d-none");
  }

  function formatBytes(bytes) {
    if (!bytes) return "0 KB";
    if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function setBusy(value) {
    busy = value;
    dropzone.classList.toggle("is-disabled", value);
    dropzone.setAttribute("aria-busy", value ? "true" : "false");
    [convertButton, inspectButton, removeButton].forEach(function (button) {
      if (button) button.disabled = value;
    });
    downloadButtons.forEach(function (button) {
      button.disabled = value || !currentResponse;
    });
  }

  function selectedFileIsPdf(file) {
    if (!file) return false;
    var name = (file.name || "").toLowerCase();
    if (name.endsWith(".pdf")) return true;
    return file.type === "application/pdf";
  }

  function releaseObjectUrl() {
    if (selectedObjectUrl) {
      URL.revokeObjectURL(selectedObjectUrl);
      selectedObjectUrl = null;
    }
    if (manualPdfFrame) manualPdfFrame.removeAttribute("src");
  }

  function clearSelection() {
    releaseObjectUrl();
    selectedFile = null;
    manualContext = null;
    currentResponse = null;
    if (selectedPanel) selectedPanel.classList.add("d-none");
    if (manualOffer) manualOffer.classList.add("d-none");
    if (resultSection) resultSection.classList.add("d-none");
    if (manualPreview) manualPreview.classList.add("d-none");
    if (downloadStatus) downloadStatus.classList.add("d-none");
    hideProgress();
    clearAlert();
    downloadButtons.forEach(function (button) {
      button.disabled = true;
    });
  }

  function chooseFile(fileList) {
    if (busy) return;
    clearAlert();
    if (!fileList || !fileList.length) return;
    if (fileList.length > 1) {
      setAlert("Please choose one PDF at a time.", "error");
      return;
    }
    var file = fileList[0];
    if (!selectedFileIsPdf(file)) {
      setAlert(messages.invalid, "error");
      return;
    }
    if (file.size > maxBytes) {
      setAlert(messages.large, "error");
      return;
    }
    if (file.size === 0) {
      setAlert(messages.invalid, "error");
      return;
    }

    releaseObjectUrl();
    selectedFile = file;
    selectedObjectUrl = URL.createObjectURL(file);
    manualContext = null;
    currentResponse = null;
    if (selectedPanel) selectedPanel.classList.remove("d-none");
    if (selectedFilename) {
      selectedFilename.textContent = file.name;
      selectedFilename.title = file.name;
    }
    if (selectedFilesize) selectedFilesize.textContent = formatBytes(file.size);
    if (manualOffer) manualOffer.classList.add("d-none");
    if (resultSection) resultSection.classList.add("d-none");
    if (manualPreview) manualPreview.classList.add("d-none");
    if (downloadStatus) downloadStatus.classList.add("d-none");
    setBusy(false);
  }

  function inspectSelected() {
    if (!selectedObjectUrl) return;
    var opened = window.open(selectedObjectUrl, "_blank", "noopener");
    if (!opened) {
      setAlert("Your browser blocked the new tab. Allow popups and try again.", "error");
      return;
    }
    setAlert(messages.inspect, "info");
  }

  function startUpload() {
    if (!selectedFile || busy) return;
    currentResponse = null;
    manualContext = null;
    if (manualOffer) manualOffer.classList.add("d-none");
    if (resultSection) resultSection.classList.add("d-none");
    if (manualPreview) manualPreview.classList.add("d-none");
    if (downloadStatus) downloadStatus.classList.add("d-none");
    if (selectedPanel) selectedPanel.classList.add("d-none");
    clearAlert();
    setBusy(true);
    showProgress(messages.uploading, 2);

    var formData = new FormData();
    formData.append("file", selectedFile, selectedFile.name);
    var xhr = new XMLHttpRequest();
    xhr.open("POST", uploadUrl, true);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    xhr.timeout = 180000;

    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable) return;
      setProgress(2 + (event.loaded / event.total) * 82);
    };
    xhr.upload.onload = function () {
      if (progressLabel) progressLabel.textContent = messages.parsing;
      setProgress(88);
    };
    xhr.onload = function () {
      var payload = null;
      try {
        payload = JSON.parse(xhr.responseText || "{}");
      } catch (err) {
        payload = null;
      }

      if (xhr.status >= 200 && xhr.status < 300 && payload && payload.success) {
        setProgress(100);
        if (progressLabel) progressLabel.textContent = messages.done;
        currentResponse = payload;
        manualContext = null;
        renderResult(payload);
        hideProgress();
        setBusy(false);
        if (resultSection.scrollIntoView) {
          resultSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
        return;
      }

      hideProgress();
      setBusy(false);
      if (selectedPanel) selectedPanel.classList.remove("d-none");
      var message = (payload && payload.message) || messages.error;
      if (xhr.status === 429) {
        setAlert(message || messages.quota, "error", {
          href: registerUrl,
          label: "Create a free account →"
        });
      } else if (xhr.status === 413) {
        setAlert(messages.large, "error");
      } else if (xhr.status === 422 && payload && payload.manual) {
        manualContext = payload;
        setAlert(payload.message || messages.empty, "error");
        if (manualOffer) manualOffer.classList.remove("d-none");
      } else if (xhr.status === 422) {
        setAlert(payload.message || messages.empty, "error");
      } else {
        setAlert(message, "error");
      }
    };
    xhr.onerror = function () {
      hideProgress();
      setBusy(false);
      if (selectedPanel) selectedPanel.classList.remove("d-none");
      setAlert(messages.error, "error");
    };
    xhr.ontimeout = function () {
      hideProgress();
      setBusy(false);
      if (selectedPanel) selectedPanel.classList.remove("d-none");
      setAlert("The conversion timed out. Please try a smaller statement.", "error");
    };
    xhr.send(formData);
  }

  function makeCellInput(type, value, label, extraClass) {
    var input = document.createElement("input");
    input.type = type;
    input.className = "cell-input" + (extraClass ? " " + extraClass : "");
    input.value = value === null || value === undefined ? "" : String(value);
    input.setAttribute("aria-label", label);
    if (type === "number") {
      input.step = "0.01";
      input.min = "0";
      input.inputMode = "decimal";
      input.placeholder = "0.00";
    }
    return input;
  }

  function appendCell(row, child) {
    var cell = document.createElement("td");
    cell.appendChild(child);
    row.appendChild(cell);
  }

  function addEditorRow(data, index) {
    data = data || {};
    var row = document.createElement("tr");
    var rowNumber = (typeof index === "number" ? index : transactionsBody.rows.length) + 1;

    appendCell(row, makeCellInput("date", data.date || "", "Date, row " + rowNumber));
    appendCell(
      row,
      makeCellInput("text", data.description || "", "Description, row " + rowNumber)
    );
    appendCell(
      row,
      makeCellInput("text", data.reference || "", "Reference, row " + rowNumber)
    );
    appendCell(
      row,
      makeCellInput("number", data.debit, "Debit, row " + rowNumber, "is-number")
    );
    appendCell(
      row,
      makeCellInput("number", data.credit, "Credit, row " + rowNumber, "is-number")
    );
    appendCell(
      row,
      makeCellInput("number", data.balance, "Balance, row " + rowNumber, "is-number")
    );

    var actionCell = document.createElement("td");
    var deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "row-delete";
    deleteButton.title = "Remove row";
    deleteButton.setAttribute("aria-label", "Remove row " + rowNumber);
    var icon = document.createElement("i");
    icon.className = "bi bi-x-lg";
    icon.setAttribute("aria-hidden", "true");
    deleteButton.appendChild(icon);
    deleteButton.addEventListener("click", function () {
      row.remove();
      updateResultCount();
    });
    actionCell.appendChild(deleteButton);
    row.appendChild(actionCell);

    transactionsBody.appendChild(row);
  }

  function renderRows(rows) {
    transactionsBody.textContent = "";
    (rows || []).forEach(function (row, index) {
      addEditorRow(row, index);
    });
    if (!transactionsBody.rows.length) {
      addEditorRow({}, 0);
    }
    updateResultCount();
  }

  function getRows() {
    var result = [];
    Array.prototype.forEach.call(transactionsBody.rows, function (row) {
      var inputs = row.querySelectorAll("input");
      if (inputs.length < 6) return;
      var item = {
        date: inputs[0].value.trim(),
        description: inputs[1].value.trim(),
        reference: inputs[2].value.trim(),
        debit: inputs[3].value.trim() || null,
        credit: inputs[4].value.trim() || null,
        balance: inputs[5].value.trim() || null
      };
      var hasData =
        item.date ||
        item.description ||
        item.reference ||
        item.debit ||
        item.credit ||
        item.balance;
      if (hasData) result.push(item);
    });
    return result;
  }

  function updateResultCount() {
    if (!resultRows) return;
    var count = getRows().length;
    resultRows.textContent = count + " " + messages.rowsLabel;
  }

  function formatQuota(quota) {
    if (!quota) return "Manual mode — no pages are charged for manual exports.";
    var text =
      quota.used +
      " of " +
      quota.limit +
      " " +
      (quota.window === "month" ? "monthly pages" : "pages in this 24h window") +
      " used.";
    if (quota.remaining > 0) {
      text += " " + quota.remaining + " remaining.";
    } else {
      text += " Allowance reached.";
    }
    return text;
  }

  function resetResultCopy() {
    if (resultTitle) resultTitle.textContent = "Review your transactions";
    if (resultToolbarTitle) {
      resultToolbarTitle.textContent = "Click any cell to correct or enrich it.";
    }
    if (resultToolbarCopy) {
      resultToolbarCopy.textContent =
        "Your edits are included in the downloaded Excel and CSV files.";
    }
  }

  function renderResult(payload) {
    clearAlert();
    manualContext = null;
    if (manualOffer) manualOffer.classList.add("d-none");
    if (manualPreview) manualPreview.classList.add("d-none");
    if (manualPdfFrame) manualPdfFrame.removeAttribute("src");
    resetResultCopy();
    resultSection.classList.remove("d-none");
    if (resultTitle) resultTitle.textContent = "Review your transactions";
    if (resultFilename) {
      resultFilename.textContent = payload.original_filename || messages.fileLabel;
      resultFilename.title = payload.original_filename || "";
    }
    if (resultBank) {
      resultBank.textContent = payload.detected_bank || "Generic / International bank";
    }
    if (resultPages) {
      var pages = payload.page_count || 0;
      resultPages.textContent =
        pages + " " + (pages === 1 ? messages.pageLabel : messages.pagesLabel);
    }
    if (resultQuota) {
      resultQuota.textContent = formatQuota(payload.quota);
    }
    renderRows(payload.rows || []);
    downloadButtons.forEach(function (button) {
      button.disabled = false;
    });
  }

  function openManualEditor() {
    if (!manualContext || !selectedObjectUrl) return;
    clearAlert();
    if (manualOffer) manualOffer.classList.add("d-none");
    currentResponse = {
      original_filename:
        manualContext.original_filename ||
        (selectedFile && selectedFile.name) ||
        "statement.pdf",
      page_count: manualContext.page_count || 0,
      rows: [],
      quota: null,
      downloads: manualContext.manual_downloads || {},
      manual: true
    };
    resultSection.classList.remove("d-none");
    if (manualPreview) manualPreview.classList.remove("d-none");
    if (manualPdfFrame) manualPdfFrame.src = selectedObjectUrl;
    if (resultTitle) resultTitle.textContent = messages.manualLabel;
    if (resultToolbarTitle) {
      resultToolbarTitle.textContent =
        "Type each transaction from the PDF, or add rows and paste values.";
    }
    if (resultToolbarCopy) {
      resultToolbarCopy.textContent =
        "Manual rows are exported through a signed, one-hour session. The uploaded PDF was already deleted from the server.";
    }
    if (resultFilename) {
      resultFilename.textContent = currentResponse.original_filename;
    }
    if (resultBank) resultBank.textContent = messages.manualLabel;
    if (resultPages) {
      var pages = currentResponse.page_count;
      resultPages.textContent =
        pages + " " + (pages === 1 ? messages.pageLabel : messages.pagesLabel);
    }
    if (resultQuota) resultQuota.textContent = formatQuota(null);
    renderRows([]);
    downloadButtons.forEach(function (button) {
      button.disabled = false;
    });
    if (addRowButton) addRowButton.focus();
    if (resultSection.scrollIntoView) {
      resultSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function filenameFromDisposition(disposition, format) {
    if (disposition) {
      var utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
      if (utf8) {
        try {
          return decodeURIComponent(utf8[1]);
        } catch (err) {
          /* fall through */
        }
      }
      var plain = /filename="?([^";]+)"?/i.exec(disposition);
      if (plain && plain[1]) return plain[1].trim();
    }
    var original = (currentResponse && currentResponse.original_filename) || "statement.pdf";
    original = original.replace(/\.pdf$/i, "");
    return original + "-transactions." + format;
  }

  function showDownloadStatus(message, isError) {
    if (!downloadStatus) return;
    downloadStatus.textContent = message;
    downloadStatus.classList.remove("d-none", "alert-error");
    if (isError) downloadStatus.classList.add("alert-error");
  }

  function saveBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    var anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1500);
  }

  function download(format) {
    if (!currentResponse || busy) return;
    var url = currentResponse.downloads && currentResponse.downloads[format];
    if (!url) {
      showDownloadStatus(messages.downloadError, true);
      return;
    }
    var rows = getRows();
    if (!rows.length) {
      showDownloadStatus("Add at least one transaction before downloading.", true);
      return;
    }

    downloadButtons.forEach(function (button) {
      button.disabled = true;
    });
    showDownloadStatus(messages.downloading, false);

    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ rows: rows })
    })
      .then(function (response) {
        if (!response.ok) {
          return response
            .json()
            .catch(function () {
              return { message: messages.downloadError };
            })
            .then(function (payload) {
              var error = new Error(payload.message || messages.downloadError);
              error.status = response.status;
              throw error;
            });
        }
        var disposition = response.headers.get("Content-Disposition");
        return response.blob().then(function (blob) {
          saveBlob(blob, filenameFromDisposition(disposition, format));
        });
      })
      .then(function () {
        showDownloadStatus(
          format === "xlsx" ? "Excel workbook downloaded." : "CSV file downloaded.",
          false
        );
      })
      .catch(function (error) {
        showDownloadStatus(
          error && error.status === 410 ? messages.expired : (error.message || messages.downloadError),
          true
        );
      })
      .finally(function () {
        downloadButtons.forEach(function (button) {
          button.disabled = false;
        });
      });
  }

  if (convertButton) convertButton.addEventListener("click", startUpload);
  if (inspectButton) inspectButton.addEventListener("click", inspectSelected);
  if (removeButton) removeButton.addEventListener("click", clearSelection);
  if (openManualButton) openManualButton.addEventListener("click", openManualEditor);

  dropzone.addEventListener("click", function (event) {
    if (busy) return;
    if (event.target === fileInput) return;
    fileInput.click();
  });
  dropzone.addEventListener("keydown", function (event) {
    if (busy) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInput.click();
    }
  });
  ["dragenter", "dragover"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (!busy) dropzone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      event.stopPropagation();
      dropzone.classList.remove("is-dragging");
    });
  });
  dropzone.addEventListener("drop", function (event) {
    if (busy) return;
    if (event.dataTransfer && event.dataTransfer.files) {
      chooseFile(event.dataTransfer.files);
    }
  });
  fileInput.addEventListener("change", function () {
    chooseFile(fileInput.files);
    fileInput.value = "";
  });

  addRowButton.addEventListener("click", function () {
    addEditorRow({}, transactionsBody.rows.length);
    updateResultCount();
    var rows = transactionsBody.rows;
    if (rows.length) {
      var last = rows[rows.length - 1];
      var firstInput = last.querySelector("input");
      if (firstInput) firstInput.focus();
    }
  });

  transactionsBody.addEventListener("input", updateResultCount);

  downloadButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      download(button.dataset.export);
    });
  });

  clearSelection();
  setBusy(false);
})();