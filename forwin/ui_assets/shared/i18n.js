    (function () {
      var STORAGE_KEY = "forwin-lang";
      var FALLBACK_LANG = "cn";
      var DICT = {
        cn: {
          "nav.books": "书本",
          "nav.tasks": "任务",
          "nav.archive": "世界档案",
          "nav.publish": "发布",
          "nav.settings": "配置",
          "brand.workspace": "ForWin Workspace",
          "brand.publisher": "ForWin Publisher",
          "home.title": "工作台",
          "home.status": "系统状态",
          "home.books": "书本",
          "home.tasks": "任务",
          "home.models": "模型",
          "home.generation": "生成默认值",
          "home.platform": "平台",
          "action.refresh": "刷新",
          "action.select_all": "全选",
          "action.select_deletable": "全选可删",
          "action.bulk_delete": "批量删除",
          "action.create_book": "新建书本",
          "action.create_task": "新建任务",
          "action.add_model": "添加模型",
          "action.save": "保存",
          "publish.title": "发布",
          "publish.extension": "扩展",
          "publish.install": "安装",
          "publish.extension_status": "扩展状态",
          "publish.upload": "上传",
          "publish.recent_jobs": "最近任务",
          "publish.back_home": "返回首页控制台",
          "publish.open_extension": "打开扩展设置",
          "publish.download_chromium": "下载扩展包（Chrome/Edge）",
          "publish.download_firefox": "下载 Firefox 扩展包",
          "lang.toggle": "切换语言"
        },
        en: {
          "nav.books": "Books",
          "nav.tasks": "Tasks",
          "nav.archive": "Archive",
          "nav.publish": "Publish",
          "nav.settings": "Settings",
          "brand.workspace": "ForWin Workspace",
          "brand.publisher": "ForWin Publisher",
          "home.title": "Workspace",
          "home.status": "System Status",
          "home.books": "Books",
          "home.tasks": "Tasks",
          "home.models": "Models",
          "home.generation": "Generation Defaults",
          "home.platform": "Platform",
          "action.refresh": "Refresh",
          "action.select_all": "Select All",
          "action.select_deletable": "Select Deletable",
          "action.bulk_delete": "Bulk Delete",
          "action.create_book": "New Book",
          "action.create_task": "New Task",
          "action.add_model": "Add Model",
          "action.save": "Save",
          "publish.title": "Publish",
          "publish.extension": "Extension",
          "publish.install": "Install",
          "publish.extension_status": "Extension Status",
          "publish.upload": "Upload",
          "publish.recent_jobs": "Recent Jobs",
          "publish.back_home": "Back to Console",
          "publish.open_extension": "Open Extension Settings",
          "publish.download_chromium": "Download Chrome/Edge Package",
          "publish.download_firefox": "Download Firefox Package",
          "lang.toggle": "Switch Language"
        }
      };

      function normalizeLang(lang) {
        return lang === "en" ? "en" : FALLBACK_LANG;
      }

      function getLang() {
        try {
          return normalizeLang(window.localStorage.getItem(STORAGE_KEY) || FALLBACK_LANG);
        } catch (error) {
          return FALLBACK_LANG;
        }
      }

      function t(key, lang) {
        var normalized = normalizeLang(lang || getLang());
        return (DICT[normalized] && DICT[normalized][key]) || (DICT[FALLBACK_LANG] && DICT[FALLBACK_LANG][key]) || key;
      }

      function setText(node, value) {
        if (typeof value === "string" && node.textContent !== value) {
          node.textContent = value;
        }
      }

      function applyLanguage() {
        var lang = getLang();
        document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
        document.querySelectorAll("[data-i18n]").forEach(function (node) {
          setText(node, t(node.getAttribute("data-i18n"), lang));
        });
        document.querySelectorAll("[data-i18n-placeholder]").forEach(function (node) {
          node.setAttribute("placeholder", t(node.getAttribute("data-i18n-placeholder"), lang));
        });
        document.querySelectorAll("[data-i18n-aria-label]").forEach(function (node) {
          node.setAttribute("aria-label", t(node.getAttribute("data-i18n-aria-label"), lang));
        });
        document.querySelectorAll("[data-lang]").forEach(function (node) {
          var active = node.getAttribute("data-lang") === lang;
          node.classList.toggle("active", active);
          node.setAttribute("aria-pressed", active ? "true" : "false");
        });
      }

      function setLang(lang) {
        var normalized = normalizeLang(lang);
        try {
          window.localStorage.setItem(STORAGE_KEY, normalized);
        } catch (error) {
          /* ignore private browsing storage failures */
        }
        applyLanguage();
        window.dispatchEvent(new CustomEvent("forwin-langchange", { detail: { lang: normalized } }));
      }

      window.FORWIN_DICT = DICT;
      window.getLang = getLang;
      window.setLang = setLang;
      window.setForWinLang = setLang;
      window.t = t;

      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", applyLanguage);
      } else {
        applyLanguage();
      }
    })();
