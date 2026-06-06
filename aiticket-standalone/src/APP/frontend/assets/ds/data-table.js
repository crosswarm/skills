/**
 * data-table.js - 通用数据表格组件
 *
 * 功能：列排序、文本搜索、客户端分页、列可见性、行选择、CSV导出
 * 用法：
 *   const table = new DSDataTable('#container', {
 *     columns: [
 *       { key: 'id', label: '编号', sortable: true, width: '80px' },
 *       { key: 'title', label: '标题', sortable: true },
 *       { key: 'status', label: '状态', render: (val) => `<span class="ds-badge">${val}</span>` },
 *     ],
 *     data: [...],
 *     pageSize: 20,
 *     searchable: true,
 *     selectable: false,
 *     onRowClick: (row) => { ... },
 *   });
 */
(function () {
  "use strict";

  function DSDataTable(containerSelector, options) {
    this.container =
      typeof containerSelector === "string"
        ? document.querySelector(containerSelector)
        : containerSelector;

    if (!this.container) {
      console.error("[DSDataTable] 容器未找到:", containerSelector);
      return;
    }

    this.options = Object.assign(
      {
        columns: [],
        data: [],
        pageSize: 20,
        searchable: true,
        selectable: false,
        exportable: true,
        emptyText: "暂无数据",
        onRowClick: null,
        onSelectionChange: null,
      },
      options
    );

    this._allData = this.options.data.slice();
    this._filteredData = this._allData;
    this._sortKey = null;
    this._sortAsc = true;
    this._currentPage = 1;
    this._searchTerm = "";
    this._selectedIds = new Set();
    this._hiddenColumns = new Set();

    this._render();
  }

  /* --- 数据操作 --- */

  DSDataTable.prototype.setData = function (data) {
    this._allData = data.slice();
    this._currentPage = 1;
    this._selectedIds.clear();
    this._applyFilters();
  };

  DSDataTable.prototype.getData = function () {
    return this._allData;
  };

  DSDataTable.prototype.getFilteredData = function () {
    return this._filteredData;
  };

  DSDataTable.prototype.getSelection = function () {
    return this._allData.filter(function (row) {
      return this._selectedIds.has(this._rowId(row));
    }.bind(this));
  };

  /* --- 内部方法 --- */

  DSDataTable.prototype._rowId = function (row) {
    return row.id || row.key || row._idx;
  };

  DSDataTable.prototype._applyFilters = function () {
    var term = this._searchTerm.toLowerCase();
    var cols = this.options.columns;

    // 搜索
    if (term) {
      this._filteredData = this._allData.filter(function (row) {
        return cols.some(function (col) {
          var val = row[col.key];
          return val != null && String(val).toLowerCase().indexOf(term) !== -1;
        });
      });
    } else {
      this._filteredData = this._allData.slice();
    }

    // 排序
    if (this._sortKey) {
      var key = this._sortKey;
      var asc = this._sortAsc;
      this._filteredData.sort(function (a, b) {
        var va = a[key];
        var vb = b[key];
        if (va == null) return 1;
        if (vb == null) return -1;
        if (typeof va === "number" && typeof vb === "number") {
          return asc ? va - vb : vb - va;
        }
        var sa = String(va).toLowerCase();
        var sb = String(vb).toLowerCase();
        if (sa < sb) return asc ? -1 : 1;
        if (sa > sb) return asc ? 1 : -1;
        return 0;
      });
    }

    this._renderBody();
    this._renderPagination();
  };

  DSDataTable.prototype._pageData = function () {
    var start = (this._currentPage - 1) * this.options.pageSize;
    return this._filteredData.slice(start, start + this.options.pageSize);
  };

  DSDataTable.prototype._totalPages = function () {
    return Math.max(1, Math.ceil(this._filteredData.length / this.options.pageSize));
  };

  /* --- 渲染 --- */

  DSDataTable.prototype._render = function () {
    var self = this;
    this.container.innerHTML = "";
    this.container.className = (this.container.className || "") + " ds-datatable-wrap";

    // 工具栏
    if (this.options.searchable || this.options.exportable) {
      var toolbar = document.createElement("div");
      toolbar.className = "ds-datatable-toolbar";

      if (this.options.searchable) {
        var searchInput = document.createElement("input");
        searchInput.type = "text";
        searchInput.className = "ds-input ds-datatable-search";
        searchInput.placeholder = "搜索...";
        searchInput.addEventListener("input", function () {
          self._searchTerm = this.value.trim();
          self._currentPage = 1;
          self._applyFilters();
        });
        toolbar.appendChild(searchInput);
      }

      var actions = document.createElement("div");
      actions.className = "ds-datatable-actions";

      if (this.options.exportable) {
        var exportBtn = document.createElement("button");
        exportBtn.className = "ds-btn ds-btn-secondary ds-btn-sm";
        exportBtn.textContent = "导出 CSV";
        exportBtn.addEventListener("click", function () {
          self.exportCSV();
        });
        actions.appendChild(exportBtn);
      }

      toolbar.appendChild(actions);
      this.container.appendChild(toolbar);
    }

    // 表格
    var tableWrap = document.createElement("div");
    tableWrap.className = "ds-datatable-scroll";
    tableWrap.style.overflowX = "auto";

    var table = document.createElement("table");
    table.className = "ds-table";

    // 表头
    var thead = document.createElement("thead");
    var headerRow = document.createElement("tr");

    if (this.options.selectable) {
      var thCheck = document.createElement("th");
      thCheck.style.width = "40px";
      var checkAll = document.createElement("input");
      checkAll.type = "checkbox";
      checkAll.addEventListener("change", function () {
        self._toggleSelectAll(this.checked);
      });
      thCheck.appendChild(checkAll);
      headerRow.appendChild(thCheck);
      this._checkAllEl = checkAll;
    }

    this.options.columns.forEach(function (col) {
      if (self._hiddenColumns.has(col.key)) return;

      var th = document.createElement("th");
      th.textContent = col.label || col.key;
      if (col.width) th.style.width = col.width;

      if (col.sortable) {
        th.setAttribute("data-sortable", "true");
        th.style.cursor = "pointer";
        th.addEventListener("click", function () {
          if (self._sortKey === col.key) {
            self._sortAsc = !self._sortAsc;
          } else {
            self._sortKey = col.key;
            self._sortAsc = true;
          }
          self._updateSortIndicators();
          self._applyFilters();
        });

        var indicator = document.createElement("span");
        indicator.className = "ds-sort-indicator";
        indicator.style.marginLeft = "4px";
        indicator.style.opacity = "0.4";
        indicator.textContent = "↕";
        th.appendChild(indicator);
        th._sortIndicator = indicator;
      }

      headerRow.appendChild(th);
      th._colKey = col.key;
    });

    thead.appendChild(headerRow);
    table.appendChild(thead);

    // 表体
    this._tbody = document.createElement("tbody");
    table.appendChild(this._tbody);

    tableWrap.appendChild(table);
    this.container.appendChild(tableWrap);

    this._thead = thead;

    // 分页
    this._paginationEl = document.createElement("div");
    this._paginationEl.className = "ds-datatable-pagination";
    this.container.appendChild(this._paginationEl);

    // 初始渲染
    this._applyFilters();
  };

  DSDataTable.prototype._renderBody = function () {
    var self = this;
    var pageData = this._pageData();
    this._tbody.innerHTML = "";

    if (pageData.length === 0) {
      var emptyRow = document.createElement("tr");
      var emptyCell = document.createElement("td");
      var colCount = this.options.columns.length - this._hiddenColumns.size;
      if (this.options.selectable) colCount++;
      emptyCell.setAttribute("colspan", colCount);
      emptyCell.className = "ds-datatable-empty";
      emptyCell.textContent = this.options.emptyText;
      emptyRow.appendChild(emptyCell);
      this._tbody.appendChild(emptyRow);
      return;
    }

    pageData.forEach(function (row, idx) {
      row._idx = (self._currentPage - 1) * self.options.pageSize + idx;
      var tr = document.createElement("tr");

      if (self.options.onRowClick) {
        tr.style.cursor = "pointer";
        tr.addEventListener("click", function (e) {
          if (e.target.type === "checkbox") return;
          self.options.onRowClick(row);
        });
      }

      if (self.options.selectable) {
        var tdCheck = document.createElement("td");
        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = self._selectedIds.has(self._rowId(row));
        checkbox.addEventListener("change", function () {
          self._toggleSelect(row, this.checked);
        });
        tdCheck.appendChild(checkbox);
        tr.appendChild(tdCheck);
      }

      self.options.columns.forEach(function (col) {
        if (self._hiddenColumns.has(col.key)) return;

        var td = document.createElement("td");
        var val = row[col.key];

        if (col.render) {
          td.innerHTML = col.render(val, row);
        } else {
          td.textContent = val != null ? String(val) : "-";
        }

        if (col.className) td.className = col.className;
        tr.appendChild(td);
      });

      self._tbody.appendChild(tr);
    });
  };

  DSDataTable.prototype._renderPagination = function () {
    var self = this;
    var total = this._totalPages();
    var current = this._currentPage;

    if (total <= 1) {
      this._paginationEl.innerHTML =
        '<span class="ds-datatable-count">' +
        this._filteredData.length + " 条记录</span>";
      return;
    }

    var html =
      '<span class="ds-datatable-count">' +
      this._filteredData.length + " 条记录，第 " +
      current + "/" + total + " 页</span>" +
      '<div class="ds-datatable-page-btns">';

    html +=
      '<button class="ds-btn ds-btn-ghost ds-btn-sm" ' +
      (current <= 1 ? "disabled" : "") +
      ' data-page="prev">&laquo; 上一页</button>';

    // 页码按钮（最多显示5个）
    var startPage = Math.max(1, current - 2);
    var endPage = Math.min(total, startPage + 4);
    startPage = Math.max(1, endPage - 4);

    for (var i = startPage; i <= endPage; i++) {
      html +=
        '<button class="ds-btn ds-btn-sm ' +
        (i === current ? "ds-btn-primary" : "ds-btn-ghost") +
        '" data-page="' + i + '">' + i + "</button>";
    }

    html +=
      '<button class="ds-btn ds-btn-ghost ds-btn-sm" ' +
      (current >= total ? "disabled" : "") +
      ' data-page="next">下一页 &raquo;</button>';

    html += "</div>";
    this._paginationEl.innerHTML = html;

    // 绑定点击
    this._paginationEl.querySelectorAll("[data-page]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var page = this.getAttribute("data-page");
        if (page === "prev") self._currentPage = Math.max(1, current - 1);
        else if (page === "next") self._currentPage = Math.min(total, current + 1);
        else self._currentPage = parseInt(page, 10);
        self._renderBody();
        self._renderPagination();
      });
    });
  };

  DSDataTable.prototype._updateSortIndicators = function () {
    var self = this;
    this._thead.querySelectorAll("th").forEach(function (th) {
      if (th._sortIndicator) {
        if (th._colKey === self._sortKey) {
          th._sortIndicator.textContent = self._sortAsc ? "↑" : "↓";
          th._sortIndicator.style.opacity = "1";
        } else {
          th._sortIndicator.textContent = "↕";
          th._sortIndicator.style.opacity = "0.4";
        }
      }
    });
  };

  /* --- 选择 --- */

  DSDataTable.prototype._toggleSelect = function (row, checked) {
    var id = this._rowId(row);
    if (checked) {
      this._selectedIds.add(id);
    } else {
      this._selectedIds.delete(id);
    }
    this._updateCheckAll();
    if (this.options.onSelectionChange) {
      this.options.onSelectionChange(this.getSelection());
    }
  };

  DSDataTable.prototype._toggleSelectAll = function (checked) {
    var self = this;
    this._pageData().forEach(function (row) {
      var id = self._rowId(row);
      if (checked) self._selectedIds.add(id);
      else self._selectedIds.delete(id);
    });
    this._renderBody();
    if (this.options.onSelectionChange) {
      this.options.onSelectionChange(this.getSelection());
    }
  };

  DSDataTable.prototype._updateCheckAll = function () {
    if (!this._checkAllEl) return;
    var pageData = this._pageData();
    var self = this;
    var allChecked =
      pageData.length > 0 &&
      pageData.every(function (row) {
        return self._selectedIds.has(self._rowId(row));
      });
    this._checkAllEl.checked = allChecked;
  };

  /* --- CSV导出 --- */

  DSDataTable.prototype.exportCSV = function (filename) {
    var cols = this.options.columns.filter(
      function (c) { return !this._hiddenColumns.has(c.key); }.bind(this)
    );

    var header = cols.map(function (c) { return '"' + (c.label || c.key) + '"'; }).join(",");
    var rows = this._filteredData.map(function (row) {
      return cols
        .map(function (c) {
          var val = row[c.key];
          if (val == null) return '""';
          return '"' + String(val).replace(/"/g, '""') + '"';
        })
        .join(",");
    });

    var csv = "\uFEFF" + header + "\n" + rows.join("\n");
    var blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename || "export.csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  /* --- 列可见性 --- */

  DSDataTable.prototype.hideColumn = function (key) {
    this._hiddenColumns.add(key);
    this._render();
  };

  DSDataTable.prototype.showColumn = function (key) {
    this._hiddenColumns.delete(key);
    this._render();
  };

  // 全局暴露
  window.DSDataTable = DSDataTable;
})();
