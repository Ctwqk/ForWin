type ForwinLanguage = "cn" | "en";

type TopbarItem = {
  key: "book" | "task" | "world" | "publish" | "config";
  labelKey: string;
  href: string;
  route: string;
  samePageOnHome?: boolean;
};

declare global {
  interface Window {
    FORWIN_TOPBAR_ITEMS?: TopbarItem[];
    getLang?: () => ForwinLanguage;
    setLang?: (language: ForwinLanguage) => void;
    setForWinLang?: (language: ForwinLanguage) => void;
    t?: (key: string, language?: ForwinLanguage) => string;
  }
}

const STORAGE_KEY = "forwin-lang";
const FALLBACK_LANG: ForwinLanguage = "cn";
const STYLE_ID = "forwin-topbar-style";
const TOPBAR_ITEMS: TopbarItem[] = [
  { key: "book", labelKey: "nav.book", href: "/", route: "/", samePageOnHome: true },
  { key: "task", labelKey: "nav.task", href: "/#task", route: "/", samePageOnHome: true },
  { key: "world", labelKey: "nav.archive", href: "/world-studio", route: "/world-studio" },
  { key: "publish", labelKey: "nav.publish", href: "/publishers", route: "/publishers" },
  { key: "config", labelKey: "nav.config", href: "/#config", route: "/", samePageOnHome: true }
];

const DICT: Record<ForwinLanguage, Record<string, string>> = {
  cn: {
    "nav.book": "书本",
    "nav.task": "任务",
    "nav.archive": "世界档案",
    "nav.publish": "发布",
    "nav.config": "配置",
    "lang.toggle": "切换语言"
  },
  en: {
    "nav.book": "Books",
    "nav.task": "Tasks",
    "nav.archive": "Archive",
    "nav.publish": "Publish",
    "nav.config": "Settings",
    "lang.toggle": "Switch Language"
  }
};

window.FORWIN_TOPBAR_ITEMS = TOPBAR_ITEMS;

function normalizeLanguage(value: string | null | undefined): ForwinLanguage {
  return value === "en" ? "en" : FALLBACK_LANG;
}

function getLang(): ForwinLanguage {
  try {
    return normalizeLanguage(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return FALLBACK_LANG;
  }
}

function translate(key: string, language: ForwinLanguage = getLang()): string {
  return DICT[language][key] || DICT[FALLBACK_LANG][key] || key;
}

function setLang(language: ForwinLanguage): void {
  const normalized = normalizeLanguage(language);
  try {
    window.localStorage.setItem(STORAGE_KEY, normalized);
  } catch {
    /* ignore private browsing storage failures */
  }
  document.documentElement.lang = normalized === "en" ? "en" : "zh-CN";
  window.dispatchEvent(new CustomEvent("forwin-langchange", { detail: { lang: normalized } }));
}

window.getLang = window.getLang || getLang;
window.setLang = window.setLang || setLang;
window.setForWinLang = window.setForWinLang || window.setLang;
window.t = window.t || translate;

function injectStyle(): void {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = [
    "forwin-topbar { display: block; position: relative; z-index: 1; }",
    ".forwin-topbar-wrap { max-width: 1380px; margin: 0 auto; padding: 24px 22px 0; box-sizing: border-box; }",
    "html { scrollbar-gutter: stable; }",
    "@media (max-width: 720px) { .forwin-topbar-wrap { padding: 16px 10px 0; } }"
  ].join("\n");
  document.head.appendChild(style);
}

function activeFromLocation(explicit: string | null): TopbarItem["key"] {
  if (explicit && TOPBAR_ITEMS.some((item) => item.key === explicit)) return explicit as TopbarItem["key"];
  const path = window.location.pathname || "/";
  const hash = (window.location.hash || "").replace(/^#/, "");
  if (path.startsWith("/world-studio")) return "world";
  if (path.startsWith("/publishers")) return "publish";
  if (path === "/" || path === "") {
    if (hash === "task") return "task";
    if (hash === "config") return "config";
    return "book";
  }
  return "book";
}

function isSamePageItem(item: TopbarItem): boolean {
  const path = window.location.pathname || "/";
  if ((path === "/" || path === "") && item.samePageOnHome) return true;
  return item.route !== "/" && path.startsWith(item.route);
}

function setHomeHash(key: TopbarItem["key"]): void {
  const nextHash = key === "book" ? "" : `#${key}`;
  if (window.location.hash !== nextHash) {
    window.history.replaceState(null, "", `${window.location.pathname}${nextHash}`);
  }
}

function syncLangButtons(root: HTMLElement): void {
  const lang = window.getLang?.() ?? FALLBACK_LANG;
  root.querySelectorAll("[data-lang]").forEach((node) => {
    const active = node.getAttribute("data-lang") === lang;
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

class ForwinTopbar extends HTMLElement {
  static get observedAttributes(): string[] {
    return ["active"];
  }

  private onRouteChange = (): void => this.render();
  private onLangChange = (): void => this.render();

  connectedCallback(): void {
    injectStyle();
    window.addEventListener("hashchange", this.onRouteChange);
    window.addEventListener("popstate", this.onRouteChange);
    window.addEventListener("forwin-langchange", this.onLangChange);
    this.render();
  }

  disconnectedCallback(): void {
    window.removeEventListener("hashchange", this.onRouteChange);
    window.removeEventListener("popstate", this.onRouteChange);
    window.removeEventListener("forwin-langchange", this.onLangChange);
  }

  attributeChangedCallback(): void {
    if (this.isConnected) this.render();
  }

  private render(): void {
    const active = activeFromLocation(this.getAttribute("active"));
    this.innerHTML = "";

    const wrap = document.createElement("div");
    wrap.className = "forwin-topbar-wrap";

    const bar = document.createElement("div");
    bar.className = "top-bar";

    const nav = document.createElement("nav");
    nav.className = "nav-tabs nav-tabs--primary";
    nav.setAttribute("aria-label", "ForWin primary navigation");

    TOPBAR_ITEMS.forEach((item) => {
      let node: HTMLAnchorElement | HTMLButtonElement;
      if (isSamePageItem(item)) {
        node = document.createElement("button");
        node.type = "button";
        node.addEventListener("click", () => this.selectItem(item));
      } else {
        node = document.createElement("a");
        node.href = item.href;
      }
      node.className = `nav-tab${item.key === active ? " active" : ""}`;
      if (item.key === active) {
        node.setAttribute("aria-current", "page");
        if (node.tagName === "BUTTON") node.setAttribute("aria-selected", "true");
      } else if (node.tagName === "BUTTON") {
        node.setAttribute("aria-selected", "false");
      }
      node.textContent = window.t?.(item.labelKey) ?? item.labelKey;
      nav.appendChild(node);
    });

    bar.appendChild(nav);
    bar.appendChild(this.buildLangToggle());
    wrap.appendChild(bar);
    this.appendChild(wrap);
    syncLangButtons(this);
  }

  private selectItem(item: TopbarItem): void {
    if (item.samePageOnHome) setHomeHash(item.key);
    this.setAttribute("active", item.key);
    this.dispatchEvent(
      new CustomEvent("forwin-tab-change", {
        detail: { key: item.key },
        bubbles: true,
        composed: true
      })
    );
  }

  private buildLangToggle(): HTMLElement {
    const group = document.createElement("div");
    group.className = "lang-toggle";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", window.t?.("lang.toggle") ?? "切换语言");

    [
      ["cn", "中"],
      ["en", "EN"]
    ].forEach(([code, label]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("data-lang", code);
      button.setAttribute("aria-pressed", code === (window.getLang?.() ?? FALLBACK_LANG) ? "true" : "false");
      button.textContent = label;
      button.addEventListener("click", () => window.setLang?.(normalizeLanguage(code)));
      group.appendChild(button);
    });

    return group;
  }
}

if (!customElements.get("forwin-topbar")) {
  customElements.define("forwin-topbar", ForwinTopbar);
}

export {};
