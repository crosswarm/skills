/**
 * PipelineDrawer — slide-in config drawer for Pipeline nodes
 * Usage: import { PipelineDrawer } from './pipeline-drawer.js'
 */
export class PipelineDrawer {
  /**
   * @param {object} opts
   * @param {string|Element} opts.container  Same container as Pipeline (for positioning)
   */
  constructor(opts = {}) {
    this._containerEl =
      typeof opts.container === "string"
        ? document.querySelector(opts.container)
        : opts.container || document.body;
    this._el = null;
    this._currentOpts = null;
    this._build();
  }

  _build() {
    this._el = document.createElement("div");
    this._el.className = "pl-drawer";
    this._el.setAttribute("role", "dialog");
    this._el.setAttribute("aria-modal", "true");
    this._containerEl.style.position = "relative";
    this._containerEl.appendChild(this._el);
  }

  /**
   * Open drawer with given node config.
   * @param {object} opts
   * @param {string} opts.title
   * @param {string} [opts.icon]
   * @param {Array} opts.fields  [{type, key, label, hint, options, value, min, max, readonly}]
   *   field types: 'toggle' | 'select' | 'number' | 'percent' | 'text' | 'textarea' | 'matrix' | 'readonly-stats'
   * @param {object} opts.values  {fieldKey: value}
   * @param {function} opts.onSave  async (values) => void
   * @param {function} [opts.onClose]  () => void
   */
  open(opts) {
    this._currentOpts = opts;
    this._el.innerHTML = this._renderHTML(opts);
    this._el.classList.add("pl-drawer-open");

    // width support
    this._el.style.width = opts.width === "wide" ? "560px" : "";

    // bind close
    this._el
      .querySelector(".pl-drawer-close")
      ?.addEventListener("click", () => this.close());
    // bind cancel
    this._el
      .querySelector('[data-action="cancel"]')
      ?.addEventListener("click", () => this.close());
    // bind save
    this._el
      .querySelector('[data-action="save"]')
      ?.addEventListener("click", () => this._handleSave());

    // Trigger onMount for subsection fields
    const fields = opts.fields || [];
    fields.forEach((f) => {
      if (f.type === "subsection" && typeof f.onMount === "function") {
        const ssEl = this._el.querySelector(
          `[data-subsection-key="${f.key || ""}"] > div:last-child`,
        );
        if (ssEl) {
          try {
            f.onMount(ssEl);
          } catch (e) {
            console.warn("[PipelineDrawer] subsection onMount failed:", e);
          }
        }
      }
    });

    // focus first focusable element
    const first = this._el.querySelector(
      'button,input,select,textarea,[tabindex="0"]',
    );
    if (first) setTimeout(() => first.focus(), 50);
  }

  close() {
    this._el.classList.remove("pl-drawer-open");
    if (this._currentOpts?.onClose) this._currentOpts.onClose();
    this._currentOpts = null;
  }

  _renderHTML(opts) {
    const fields = opts.fields || [];
    const values = opts.values || {};

    const fieldsHtml = fields
      .map((f) => this._renderField(f, values[f.key]))
      .join("");

    return `
      <div class="pl-drawer-header">
        ${opts.icon ? `<span aria-hidden="true" style="width:24px;height:24px;color:var(--ds-mod-board)">${opts.icon}</span>` : ""}
        <span class="pl-drawer-title">${opts.title || ""}</span>
        <button class="pl-drawer-close" aria-label="关闭">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
      <div class="pl-drawer-body">${fieldsHtml}</div>
      <div class="pl-drawer-footer">
        <button class="ds-btn ds-btn-ghost ds-btn-sm" data-action="cancel">取消</button>
        <button class="ds-btn ds-btn-primary ds-btn-sm" data-action="save">保存</button>
      </div>
    `;
  }

  _renderField(f, currentValue) {
    if (f.type === "subsection") {
      const uid = "pl-ss-" + (f.key || Math.random().toString(36).slice(2));
      return `<div class="pl-field pl-field-subsection" data-subsection-key="${f.key || ""}">
        <div class="pl-field-label" style="font-weight:600;border-bottom:1px solid var(--ds-border-subtle,#f1f5f9);padding-bottom:6px;margin-bottom:8px;cursor:pointer;display:flex;align-items:center;justify-content:space-between"
             onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'':'none'"
        >
            ${f.label}
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><polyline points="6 9 12 15 18 9"/></svg>
        </div>
        <div id="${uid}" style="min-height:40px">${f.html || '<div style="color:var(--ds-text-muted,#94a3b8);font-size:12px;padding:8px 0">加载中...</div>'}</div>
      </div>`;
    }
    if (f.type === "readonly-stats") {
      const rows = Object.entries(f.value || {})
        .map(
          ([k, v]) =>
            `<div class="pl-stats-row"><span>${k}</span><span class="pl-stats-val">${v}</span></div>`,
        )
        .join("");
      return `<div class="pl-field"><div class="pl-field-label">${f.label}</div><div class="pl-stats-block">${rows || '<span style="font-size:11px;color:var(--ds-text-muted)">暂无数据</span>'}</div></div>`;
    }
    if (f.type === "toggle") {
      const checked =
        currentValue === true || currentValue === "true" ? "checked" : "";
      return `<div class="pl-field pl-field-toggle">
        <span class="pl-field-label" style="margin-bottom:0">${f.label}</span>
        <label class="ds-switch"><input type="checkbox" data-key="${f.key}" ${checked}><span class="ds-switch-slider"></span></label>
      </div>`;
    }
    if (f.type === "select") {
      const opts = (f.options || [])
        .map((o) => {
          const val = typeof o === "object" ? o.value : o;
          const lbl = typeof o === "object" ? o.label : o;
          const sel = val === currentValue ? "selected" : "";
          return `<option value="${val}" ${sel}>${lbl}</option>`;
        })
        .join("");
      return `<div class="pl-field"><div class="pl-field-label">${f.label}</div>
        <select class="ds-select" data-key="${f.key}" style="width:100%">${opts}</select>
        ${f.hint ? `<div class="pl-field-hint">${f.hint}</div>` : ""}</div>`;
    }
    if (f.type === "number" || f.type === "percent") {
      const suffix = f.type === "percent" ? "%" : "";
      return `<div class="pl-field"><div class="pl-field-label">${f.label}</div>
        <div style="display:flex;align-items:center;gap:6px">
          <input type="number" class="ds-input" data-key="${f.key}" value="${currentValue ?? ""}"
            min="${f.min ?? ""}" max="${f.max ?? ""}" step="${f.step || (f.type === "percent" ? 1 : "any")}"
            style="width:80px">
          ${suffix ? `<span style="font-size:12px;color:var(--ds-text-muted)">${suffix}</span>` : ""}
        </div>
        ${f.hint ? `<div class="pl-field-hint">${f.hint}</div>` : ""}</div>`;
    }
    if (f.type === "textarea") {
      return `<div class="pl-field"><div class="pl-field-label">${f.label}</div>
        <textarea class="ds-input" data-key="${f.key}" rows="3" style="width:100%;resize:vertical">${currentValue ?? ""}</textarea>
        ${f.hint ? `<div class="pl-field-hint">${f.hint}</div>` : ""}</div>`;
    }
    // default: text
    return `<div class="pl-field"><div class="pl-field-label">${f.label}</div>
      <input type="text" class="ds-input" data-key="${f.key}" value="${currentValue ?? ""}" style="width:100%">
      ${f.hint ? `<div class="pl-field-hint">${f.hint}</div>` : ""}</div>`;
  }

  _collectValues() {
    const values = {};
    this._el.querySelectorAll("[data-key]").forEach((el) => {
      // skip elements inside subsection content (managed by their own APIs)
      if (el.closest(".pl-field-subsection")) return;
      const key = el.dataset.key;
      if (el.type === "checkbox") {
        values[key] = el.checked;
      } else if (el.type === "number") {
        values[key] = el.value === "" ? null : Number(el.value);
      } else {
        values[key] = el.value;
      }
    });
    return values;
  }

  async _handleSave() {
    if (!this._currentOpts?.onSave) {
      this.close();
      return;
    }
    const values = this._collectValues();
    const btn = this._el.querySelector('[data-action="save"]');
    if (btn) {
      btn.disabled = true;
      btn.textContent = "保存中…";
    }
    try {
      await this._currentOpts.onSave(values);
      this.close();
    } catch (e) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "保存";
      }
      console.error("[PipelineDrawer] save failed:", e);
    }
  }

  static fieldTypes() {
    return [
      "toggle",
      "select",
      "number",
      "percent",
      "text",
      "textarea",
      "matrix",
      "readonly-stats",
      "subsection",
    ];
  }
}
