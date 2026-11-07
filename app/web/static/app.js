/* 小红书发布助手 - 前端逻辑(Vue3) */
const { createApp, ref, computed, onMounted } = Vue;

const app = createApp({
  setup() {
    // ---------- 全局 ----------
    const tab = ref("accounts");
    const tabs = [
      { key: "accounts", icon: "👤", label: "账号管理" },
      { key: "notes", icon: "📝", label: "笔记管理" },
      { key: "obsidian", icon: "🗂", label: "Obsidian" },
      { key: "stats", icon: "📊", label: "内容数据" },
      { key: "logs", icon: "📜", label: "运行日志" },
      { key: "settings", icon: "⚙️", label: "设置" },
    ];
    const toasts = ref([]);
    let toastId = 0;
    function toast(msg, type = "info", ms = 3500) {
      const id = ++toastId;
      toasts.value.push({ id, msg, type });
      setTimeout(() => (toasts.value = toasts.value.filter((t) => t.id !== id)), ms);
    }
    async function api(url, opts = {}) {
      const res = await fetch(url, opts);
      if (!res.ok) {
        let detail = res.statusText;
        try {
          const j = await res.json();
          detail = j.detail || JSON.stringify(j);
        } catch (e) {}
        throw new Error(detail);
      }
      return res.json();
    }
    const statusMap = {
      pending: "待扫码", logging_in: "登录中", online: "在线", expired: "已失效",
      draft: "草稿", queued: "排队中", publishing: "发布中", published: "已发布",
      failed: "失败", platform_scheduled: "平台定时", overdue: "逾期暂停", verifying: "待核对",
    };
    const statusText = (s) => statusMap[s] || s;

    const noteStatusText = (note) => {
      const status = typeof note === "string" ? note : note.status;
      if (status === "pending") return typeof note === "object" && note.schedule_mode !== "immediate" ? "等待定时发布" : "待发布";
      return ({
        draft: "草稿",
        queued: "排队中",
        publishing: "发布中",
        published: "已发布",
        failed: "发布失败",
        platform_scheduled: "已提交平台定时",
        overdue: "计划已逾期(已暂停)",
        verifying: "待平台核对",
      })[status] || status;
    };
    // ---------- 账号 ----------
    const accounts = ref([]);
    async function loadAccounts() {
      accounts.value = await api("/api/accounts");
    }
    const addModal = ref(false);
    const addStep2 = ref(false);
    const newName = ref("");
    const qrData = ref("");
    const qrMessage = ref("");
    let qrTimer = null;
    let addAccountId = null;

    function openAddAccount() {
      newName.value = "";
      addStep2.value = false;
      addModal.value = true;
    }
    async function createAccount() {
      try {
        const r = await api("/api/accounts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: newName.value.trim() }),
        });
        addAccountId = r.id;
        addStep2.value = true;
        qrData.value = "";
        qrMessage.value = "正在打开浏览器…";
        pollQr();
        loadAccounts();
      } catch (e) {
        toast(e.message, "error");
      }
    }
    function pollQr() {
      clearInterval(qrTimer);
      qrTimer = setInterval(async () => {
        if (!addModal.value) return clearInterval(qrTimer);
        try {
          const st = await api(`/api/accounts/${addAccountId}/qr`);
          if (st.qr) qrData.value = st.qr;
          qrMessage.value = st.message || qrMessage.value;
          if (st.status === "success") {
            clearInterval(qrTimer);
            toast("登录成功 🎉");
            addModal.value = false;
            loadAccounts();
          } else if (["timeout", "failed"].includes(st.status)) {
            clearInterval(qrTimer);
            toast(st.message || "登录未完成", "warn");
            addModal.value = false;
            loadAccounts();
          }
        } catch (e) {}
      }, 2000);
    }
    async function reloginAccount(a) {
      try {
        await api(`/api/accounts/${a.id}/login`, { method: "POST" });
        addAccountId = a.id;
        qrData.value = "";
        qrMessage.value = "正在打开浏览器…";
        addStep2.value = true;
        addModal.value = true;
        pollQr();
      } catch (e) {
        toast(e.message, "warn");
      }
    }
    async function checkAccount(a) {
      toast("正在检测…");
      try {
        const r = await api(`/api/accounts/${a.id}/check`, { method: "POST" });
        if (r.ok === null) {
          toast(r.message || "该账号正在执行任务,请稍后再检测", "info");
          return;
        }
        toast(r.ok ? "登录态有效 ✅" : "登录态已失效,请重新扫码", r.ok ? "info" : "warn");
        loadAccounts();
      } catch (e) {
        toast(e.message, "error");
      }
    }
    async function collectAccount(a) {
      try {
        await api(`/api/accounts/${a.id}/collect`, { method: "POST" });
        toast("已加入采集队列,任务会与发布错峰执行");
      } catch (e) {
        toast(e.message, "error");
      }
    }
    const accModal = ref(false);
    const accForm = ref({});
    function editAccount(a) {
      accForm.value = { ...a };
      accModal.value = true;
    }
    async function saveAccount() {
      try {
        await api(`/api/accounts/${accForm.value.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: accForm.value.name,
            proxy: accForm.value.proxy,
            daily_limit: accForm.value.daily_limit,
          }),
        });
        accModal.value = false;
        toast("已保存");
        loadAccounts();
      } catch (e) {
        toast(e.message, "error");
      }
    }
    async function removeAccount(a) {
      if (!confirm(`删除账号「${a.name}」?其浏览器环境与笔记记录会一并删除。`)) return;
      await api(`/api/accounts/${a.id}`, { method: "DELETE" });
      toast("已删除");
      loadAccounts();
    }

    // ---------- 笔记 ----------
    const allNotes = ref([]);
    const noteFilter = ref("");
    const noteAccountFilter = ref("");
    const notes = computed(() => allNotes.value.filter((n) =>
      (!noteFilter.value || n.status === noteFilter.value) &&
      (!noteAccountFilter.value || String(n.account_id) === String(noteAccountFilter.value))
    ));
    const queueCount = computed(
      () => allNotes.value.filter((n) => ["pending", "queued", "publishing"].includes(n.status)).length
    );
    async function loadNotes() {
      allNotes.value = await api("/api/notes");
    }
    const noteBadgeClass = (s) => ({
      published: "bg-green-100 text-green-700",
      failed: "bg-red-100 text-red-600",
      publishing: "bg-blue-100 text-blue-600",
      queued: "bg-amber-100 text-amber-700",
      pending: "bg-slate-100 text-slate-500",
      platform_scheduled: "bg-indigo-100 text-indigo-600",
      draft: "bg-slate-100 text-slate-600 border border-slate-300",
      overdue: "bg-orange-100 text-orange-700 font-semibold",
      verifying: "bg-purple-100 text-purple-700 font-semibold",
    }[s] || "bg-slate-100");

    const createModal = ref(false);
    const submitting = ref(false);
    const form = ref({ account_id: 0, title: "", content: "", note_type: "image", tags: "", when_mode: "draft", scheduled_at: "" });
    const fileList = ref([]);
    const filePreviews = ref([]);
    function openCreate() {
      if (!accounts.value.length) {
        toast("请先在账号管理中添加账号", "warn");
        return;
      }
      const defaultAcc = accounts.value.find((a) => a.status === "online") || accounts.value[0];
      form.value = {
        account_id: defaultAcc.id,
        title: "",
        content: "",
        note_type: "image",
        tags: "",
        when_mode: "draft",
        scheduled_at: "",
      };
      fileList.value = [];
      filePreviews.value = [];
      createModal.value = true;
    }
    function onFiles(ev) {
      fileList.value = [...ev.target.files];
      if (form.value.note_type === "image") {
        filePreviews.value = fileList.value.slice(0, 8).map((f) => URL.createObjectURL(f));
      } else filePreviews.value = [];
    }
    async function saveDraftOnly() {
      if (!form.value.title.trim()) {
        toast("请至少填写标题", "warn");
        return;
      }
      submitting.value = true;
      try {
        const fd = new FormData();
        fd.append("account_id", form.value.account_id);
        fd.append("title", form.value.title.trim());
        fd.append("content", form.value.content);
        fd.append("note_type", form.value.note_type);
        fd.append("tags", form.value.tags);
        fd.append("save_as_draft", "1");
        fileList.value.forEach((f) => fd.append("files", f));
        await api("/api/notes", { method: "POST", body: fd });
        createModal.value = false;
        toast("草稿已安全保存 (status: draft)");
        loadNotes();
      } catch (e) {
        toast(e.message, "error");
      } finally {
        submitting.value = false;
      }
    }
    async function submitNote() {
      const acc = accounts.value.find((a) => a.id === form.value.account_id);
      if (!acc || acc.status !== "online") {
        toast("安排发布需要归属账号处于在线状态,请先重新扫码登录", "warn");
        return;
      }
      if (!fileList.value.length) {
        toast("安排发布必须至少上传一个图片或视频媒体文件", "warn");
        return;
      }
      submitting.value = true;
      try {
        if (form.value.when_mode === "schedule" && !form.value.scheduled_at) throw new Error("请选择定时发布时间");
        const fd = new FormData();
        fd.append("account_id", form.value.account_id);
        fd.append("title", form.value.title.trim());
        fd.append("content", form.value.content);
        fd.append("note_type", form.value.note_type);
        fd.append("tags", form.value.tags);
        fd.append("save_as_draft", "0");
        if (form.value.when_mode === "schedule") fd.append("scheduled_at", form.value.scheduled_at.replace("T", " "));
        fileList.value.forEach((f) => fd.append("files", f));
        const r = await api("/api/notes", { method: "POST", body: fd });
        createModal.value = false;
        toast(r.schedule_mode === "platform" ? "已创建,并已排入小红书官方定时" : "已创建并加入后台发布队列");
        loadNotes();
      } catch (e) {
        toast(e.message, "error");
      } finally {
        submitting.value = false;
      }
    }
    async function publishNow(n) {
      try {
        const r = await api(`/api/notes/${n.id}/publish-now`, { method: "POST" });
        toast(r.message);
        loadNotes();
      } catch (e) {
        toast(e.message, "error");
      }
    }
    const editModal = ref(false);
    const editForm = ref({});
    function editNote(n) {
      editForm.value = {
        id: n.id,
        title: n.title,
        content: n.content,
        tagsStr: (n.tags || []).join(","),
        scheduled_at: (n.scheduled_at || "").slice(0, 16).replace(" ", "T"),
      };
      editModal.value = true;
    }
    async function saveNote() {
      try {
        await api(`/api/notes/${editForm.value.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: editForm.value.title,
            content: editForm.value.content,
            tags: editForm.value.tagsStr.split(/[,，]/).map((x) => x.trim()).filter(Boolean),
            scheduled_at: editForm.value.scheduled_at ? editForm.value.scheduled_at.replace("T", " ") : "",
          }),
        });
        editModal.value = false;
        toast("已保存");
        loadNotes();
      } catch (e) {
        toast(e.message, "error");
      }
    }
    async function removeNote(n) {
      if (!confirm(`删除笔记「${n.title}」?`)) return;
      await api(`/api/notes/${n.id}`, { method: "DELETE" });
      loadNotes();
    }

    // ---------- Obsidian ----------
    const obsidianDir = ref("");
    const drafts = ref([]);
    const selectedDrafts = ref([]);
    const obsidianAccountId = ref(0);
    const obsidianMove = ref(true);
    async function loadSettings() {
      const s = await api("/api/settings");
      obsidianDir.value = s.obsidian_dir || "";
    }
    async function saveObsidianDir() {
      await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ obsidian_dir: obsidianDir.value }),
      });
      toast("路径已保存");
    }
    async function scanObsidian() {
      try {
        const r = await api(`/api/obsidian/scan?dir_path=${encodeURIComponent(obsidianDir.value)}`);
        drafts.value = r.drafts;
        selectedDrafts.value = r.drafts.filter((d) => !d.warnings.length && d.images.length).map((d) => d.path);
        toast(`扫描到 ${r.drafts.length} 篇草稿`);
      } catch (e) {
        toast(e.message, "error");
      }
    }
    async function importObsidian() {
      try {
        const r = await api("/api/obsidian/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            dir_path: obsidianDir.value,
            files: selectedDrafts.value,
            account_id: obsidianAccountId.value,
            move_after: obsidianMove.value,
          }),
        });
        const okCount = r.results.filter((x) => x.ok).length;
        toast(`导入完成:${okCount}/${r.results.length} 篇成功`);
        drafts.value = [];
        selectedDrafts.value = [];
        loadNotes();
      } catch (e) {
        toast(e.message, "error");
      }
    }

    // ---------- 数据 ----------
    const statsOverview = ref([]);
    const statsNotes = ref([]);
    const statsAccountFilter = ref(0);
    async function loadStats() {
      statsOverview.value = await api("/api/stats/overview");
      statsNotes.value = await api(
        "/api/stats/notes" + (statsAccountFilter.value ? `?account_id=${statsAccountFilter.value}` : "")
      );
    }
    async function refreshStats() {
      try {
        const r = await api("/api/stats/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        toast(r.message);
      } catch (e) {
        toast(e.message, "error");
      }
    }

    // ---------- 日志 ----------
    const logs = ref([]);
    async function loadLogs() {
      logs.value = await api("/api/logs");
    }

    // ---------- 设置(F-08.2) ----------
    const settingsForm = ref({ obsidian_dir: "", published_dir: "", collect_times: "", headless: true });
    async function loadSettingsForm() {
      const s = await api("/api/settings");
      settingsForm.value = {
        obsidian_dir: s.obsidian_dir || "",
        published_dir: s.published_dir || "",
        collect_times: (s.collect_times || []).join(","),
        headless: !!s.headless,
      };
      obsidianDir.value = s.obsidian_dir || "";
    }
    async function saveSettings() {
      try {
        await api("/api/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            obsidian_dir: settingsForm.value.obsidian_dir,
            published_dir: settingsForm.value.published_dir,
            collect_times: settingsForm.value.collect_times.split(/[,，]/).map((x) => x.trim()).filter(Boolean),
            headless: settingsForm.value.headless,
          }),
        });
        toast("设置已保存,采集计划已同步更新");
        await loadSettingsForm();
      } catch (e) {
        toast(e.message, "error");
      }
    }
    function switchTab(key) {
      tab.value = key;
      if (key === "settings") loadSettingsForm();
      if (key === "stats") loadStats();
    }

    // ---------- F-08.1: 账号筛选点击立即刷新 ----------
    function filterAccount(id) {
      statsAccountFilter.value = id;
      loadStats();
    }

    // ---------- 轮询 ----------
    setInterval(() => {
      loadAccounts();
      loadNotes();
      if (tab.value === "stats") loadStats();
      if (tab.value === "logs") loadLogs();
    }, 8000);

    onMounted(async () => {
      await Promise.all([loadAccounts(), loadNotes(), loadSettings(), loadStats(), loadLogs()]);
    });

    return {
      tab, tabs, toasts, toast, api, statusText, noteStatusText,
      accounts, addModal, addStep2, newName, qrData, qrMessage,
      openAddAccount, createAccount, reloginAccount, checkAccount, collectAccount,
      accModal, accForm, editAccount, saveAccount, removeAccount,
      notes, noteFilter, noteAccountFilter, queueCount, loadNotes, noteBadgeClass,
      createModal, submitting, form, fileList, filePreviews, openCreate, onFiles, submitNote,
      publishNow, editModal, editForm, editNote, saveNote, removeNote,
      obsidianDir, drafts, selectedDrafts, obsidianAccountId, obsidianMove,
      loadSettings, saveObsidianDir, scanObsidian, importObsidian,
      statsOverview, statsNotes, statsAccountFilter, loadStats, refreshStats,
      logs, loadLogs,
      settingsForm, loadSettingsForm, saveSettings, switchTab, filterAccount,
    };
  },
});
app.component("chinese-datetime", {
  props: ["modelValue"], emits: ["update:modelValue"],
  computed: {
    parts() {
      const d = this.modelValue ? new Date(this.modelValue.replace(" ", "T")) : new Date();
      return [d.getFullYear(), d.getMonth()+1, d.getDate(), d.getHours(), d.getMinutes()];
    },
    years() { const y = new Date().getFullYear(); return Array.from(new Set([this.parts[0], y, y+1, y+2])).sort(); },
    days() { return new Date(this.parts[0], this.parts[1], 0).getDate(); }
  },
  methods: {
    update(index, event) {
      const p = [...this.parts]; p[index] = Number(event.target.value);
      p[2] = Math.min(p[2], new Date(p[0], p[1], 0).getDate());
      const pad = n => String(n).padStart(2, "0");
      this.$emit("update:modelValue", `${p[0]}-${pad(p[1])}-${pad(p[2])}T${pad(p[3])}:${pad(p[4])}`);
    },
    today() { const d = new Date(); const pad = n => String(n).padStart(2,"0"); this.$emit("update:modelValue", `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`); },
  },
  template: `<div class="flex flex-wrap items-center gap-2 text-sm">
    <select aria-label="年" class="inp !w-auto" :value="parts[0]" @change="update(0,$event)"><option v-for="y in years" :value="y">{{y}}年</option></select>
    <select aria-label="月" class="inp !w-auto" :value="parts[1]" @change="update(1,$event)"><option v-for="m in 12" :value="m">{{m}}月</option></select>
    <select aria-label="日" class="inp !w-auto" :value="parts[2]" @change="update(2,$event)"><option v-for="d in days" :value="d">{{d}}日</option></select>
    <select aria-label="时" class="inp !w-auto" :value="parts[3]" @change="update(3,$event)"><option v-for="h in 24" :value="h-1">{{String(h-1).padStart(2,'0')}}时</option></select>
    <select aria-label="分" class="inp !w-auto" :value="parts[4]" @change="update(4,$event)"><option v-for="m in 60" :value="m-1">{{String(m-1).padStart(2,'0')}}分</option></select>
    <button type="button" class="btn btn-ghost btn-sm" @click="today">使用当前时间</button>
    <button type="button" class="btn btn-ghost btn-sm" @click="$emit('update:modelValue','')">清空</button>
    <span v-if="!modelValue" class="text-xs text-slate-400">尚未选择时间</span>
  </div>`
});
app.mount("#app");
