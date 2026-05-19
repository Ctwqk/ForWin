    (function () {
      if (customElements.get("forwin-topbar")) return;

      var STYLE_ID = "forwin-topbar-style";
      var FORWIN_TOPBAR_ITEMS = [
        { key: "book", labelKey: "nav.book", href: "/", route: "/", samePageOnHome: true },
        { key: "task", labelKey: "nav.task", href: "/#task", route: "/", samePageOnHome: true },
        { key: "world", labelKey: "nav.archive", href: "/world-studio", route: "/world-studio" },
        { key: "publish", labelKey: "nav.publish", href: "/publishers", route: "/publishers" },
        { key: "config", labelKey: "nav.config", href: "/#config", route: "/", samePageOnHome: true },
      ];

      window.FORWIN_TOPBAR_ITEMS = FORWIN_TOPBAR_ITEMS;

      function injectStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var style = document.createElement("style");
        style.id = STYLE_ID;
        style.textContent = [
          "forwin-topbar { display: block; position: relative; z-index: 1; }",
          ".forwin-topbar-wrap { max-width: 1380px; margin: 0 auto; padding: 24px 22px 0; box-sizing: border-box; }",
          "html { scrollbar-gutter: stable; }",
          "@media (max-width: 720px) { .forwin-topbar-wrap { padding: 16px 10px 0; } }",
        ].join("\n");
        document.head.appendChild(style);
      }

      function translate(key) {
        return typeof window.t === "function" ? window.t(key) : key;
      }

      function getCurrentLang() {
        return typeof window.getLang === "function" ? window.getLang() : "cn";
      }

      function setCurrentLang(lang) {
        if (typeof window.setLang === "function") {
          window.setLang(lang);
        }
      }

      function activeFromLocation(explicit) {
        if (explicit) return explicit;
        var path = window.location.pathname || "/";
        var hash = (window.location.hash || "").replace(/^#/, "");
        if (path.indexOf("/world-studio") === 0) return "world";
        if (path.indexOf("/publishers") === 0) return "publish";
        if (path === "/" || path === "") {
          if (hash === "task") return "task";
          if (hash === "config") return "config";
          return "book";
        }
        return "book";
      }

      function isSamePageItem(item) {
        var path = window.location.pathname || "/";
        if ((path === "/" || path === "") && item.samePageOnHome) return true;
        return item.route !== "/" && path.indexOf(item.route) === 0;
      }

      function setHomeHash(key) {
        var nextHash = key === "book" ? "" : "#" + key;
        if (window.location.hash !== nextHash) {
          window.history.replaceState(null, "", window.location.pathname + nextHash);
        }
      }

      function syncLangButtons(root) {
        var lang = getCurrentLang();
        root.querySelectorAll("[data-lang]").forEach(function (node) {
          var active = node.getAttribute("data-lang") === lang;
          node.classList.toggle("active", active);
          node.setAttribute("aria-pressed", active ? "true" : "false");
        });
      }

      class ForwinTopbar extends HTMLElement {
        static get observedAttributes() {
          return ["active"];
        }

        connectedCallback() {
          injectStyle();
          this._onRouteChange = () => this._render();
          this._onLangChange = () => this._render();
          window.addEventListener("hashchange", this._onRouteChange);
          window.addEventListener("popstate", this._onRouteChange);
          window.addEventListener("forwin-langchange", this._onLangChange);
          this._render();
        }

        disconnectedCallback() {
          window.removeEventListener("hashchange", this._onRouteChange);
          window.removeEventListener("popstate", this._onRouteChange);
          window.removeEventListener("forwin-langchange", this._onLangChange);
        }

        attributeChangedCallback() {
          if (this.isConnected) {
            this._render();
          }
        }

        _render() {
          var active = activeFromLocation(this.getAttribute("active"));
          this.innerHTML = "";

          var wrap = document.createElement("div");
          wrap.className = "forwin-topbar-wrap";

          var bar = document.createElement("div");
          bar.className = "top-bar";

          var nav = document.createElement("nav");
          nav.className = "nav-tabs nav-tabs--primary";
          nav.setAttribute("aria-label", "ForWin primary navigation");

          FORWIN_TOPBAR_ITEMS.forEach((item) => {
            var node;
            if (isSamePageItem(item)) {
              node = document.createElement("button");
              node.type = "button";
              node.addEventListener("click", () => this._selectItem(item));
            } else {
              node = document.createElement("a");
              node.href = item.href;
            }
            node.className = "nav-tab" + (item.key === active ? " active" : "");
            if (item.key === active) {
              node.setAttribute("aria-current", "page");
              if (node.tagName === "BUTTON") {
                node.setAttribute("aria-selected", "true");
              }
            } else if (node.tagName === "BUTTON") {
              node.setAttribute("aria-selected", "false");
            }
            node.textContent = translate(item.labelKey);
            nav.appendChild(node);
          });

          bar.appendChild(nav);
          bar.appendChild(this._buildLangToggle());
          wrap.appendChild(bar);
          this.appendChild(wrap);
          syncLangButtons(this);
        }

        _selectItem(item) {
          if (item.samePageOnHome) {
            setHomeHash(item.key);
          }
          this.setAttribute("active", item.key);
          this.dispatchEvent(
            new CustomEvent("forwin-tab-change", {
              detail: { key: item.key },
              bubbles: true,
              composed: true,
            })
          );
        }

        _buildLangToggle() {
          var group = document.createElement("div");
          group.className = "lang-toggle";
          group.setAttribute("role", "group");
          group.setAttribute("aria-label", translate("lang.toggle"));

          [
            ["cn", "中"],
            ["en", "EN"],
          ].forEach(function (entry) {
            var button = document.createElement("button");
            button.type = "button";
            button.setAttribute("data-lang", entry[0]);
            button.setAttribute("aria-pressed", entry[0] === getCurrentLang() ? "true" : "false");
            button.textContent = entry[1];
            button.addEventListener("click", function () {
              setCurrentLang(entry[0]);
            });
            group.appendChild(button);
          });

          return group;
        }
      }

      customElements.define("forwin-topbar", ForwinTopbar);
    })();
