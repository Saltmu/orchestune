(function () {
  "use strict";

  var STORAGE_KEY = "orchestune-lang";
  var COPY_LABEL = { en: "Copy", ja: "コピー" };
  var COPIED_LABEL = { en: "Copied!", ja: "コピーしました" };

  function detectLang() {
    var stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "en" || stored === "ja") return stored;
    return navigator.language && navigator.language.toLowerCase().indexOf("ja") === 0 ? "ja" : "en";
  }

  function applyLang(lang) {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-lang-btn]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-lang-btn") === lang);
    });
    document.querySelectorAll(".copy-btn").forEach(function (btn) {
      btn.textContent = btn.classList.contains("copied") ? COPIED_LABEL[lang] : COPY_LABEL[lang];
    });
    localStorage.setItem(STORAGE_KEY, lang);
  }

  document.querySelectorAll("[data-lang-btn]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      applyLang(btn.getAttribute("data-lang-btn"));
    });
  });

  applyLang(detectLang());

  var navToggle = document.getElementById("nav-toggle");
  var siteNav = document.getElementById("site-nav");
  if (navToggle && siteNav) {
    navToggle.addEventListener("click", function () {
      var open = siteNav.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", String(open));
    });
    siteNav.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () {
        siteNav.classList.remove("open");
        navToggle.setAttribute("aria-expanded", "false");
      });
    });
  }

  document.querySelectorAll(".copy-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy") || "";
      var lang = document.documentElement.lang === "ja" ? "ja" : "en";
      var reset = function () {
        btn.classList.remove("copied");
        btn.textContent = COPY_LABEL[lang];
      };
      var markCopied = function () {
        btn.classList.add("copied");
        btn.textContent = COPIED_LABEL[lang];
        setTimeout(reset, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(markCopied, function () {
          fallbackCopy(text);
          markCopied();
        });
      } else {
        fallbackCopy(text);
        markCopied();
      }
    });
  });

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch (e) {
      /* no-op */
    }
    document.body.removeChild(ta);
  }
})();
