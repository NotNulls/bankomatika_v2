/** Dashboard helpers: copy API keys and confirm destructive actions. */
(function () {
  "use strict";

  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      var value = target.value || target.textContent || "";
      var done = function () {
        var original = button.textContent;
        button.textContent = "Copied";
        setTimeout(function () {
          button.textContent = original;
        }, 1800);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(done).catch(function () {
          target.select();
        });
      } else {
        target.focus();
        target.select();
        try {
          document.execCommand("copy");
          done();
        } catch (err) {
          /* selection remains for manual copy */
        }
      }
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    });
  });
})();
