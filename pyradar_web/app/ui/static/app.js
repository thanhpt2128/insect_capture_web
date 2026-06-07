let resultsSocket = null;

async function fetchJson(url, options) {
	const res = await fetch(url, options);
	if (!res.ok) {
		const raw = await res.text();
		let detail = raw || res.statusText;
		if (raw) {
			try {
				const parsed = JSON.parse(raw);
				detail = parsed.detail || raw;
			} catch {
				// ignore
			}
		}
		throw new Error(detail || res.statusText);
	}
	return res.json();
}

function setStatus(text) {
	const el = document.getElementById("systemStatus");
	if (el) el.textContent = String(text || "");
}

function appendResultLine(obj) {
	const out = document.getElementById("resultOut");
	if (!out) return;
	const line = JSON.stringify(obj, null, 2);
	out.textContent = (out.textContent ? out.textContent + "\n\n" : "") + line;
	out.scrollTop = out.scrollHeight;
}

async function loadComPorts() {
	const select = document.getElementById("comPortSelect");
	const msg = document.getElementById("comMessage");
	msg.textContent = "Đang quét...";

	try {
		const data = await fetchJson("/com/list");
		const ports = data.ports || [];
		select.innerHTML = "";

		if (!ports.length) {
			const opt = document.createElement("option");
			opt.value = "";
			opt.textContent = "Không tìm thấy cổng COM";
			select.appendChild(opt);
			msg.textContent = "Không tìm thấy cổng COM.";
			return;
		}

		ports.forEach((p) => {
			const opt = document.createElement("option");
			opt.value = p.device;
			opt.textContent = p.description ? `${p.device} — ${p.description}` : p.device;
			select.appendChild(opt);
		});

		msg.textContent = `Tìm thấy ${ports.length} cổng.`;
	} catch (err) {
		msg.textContent = `Quét thất bại: ${err.message}`;
	}
}

async function loadCfgFiles() {
	const select = document.getElementById("cfgSelect");
	const msg = document.getElementById("cfgMessage");
	msg.textContent = "Đang tải danh sách cấu hình...";

	try {
		const data = await fetchJson("/config/list");
		const files = data.files || [];
		select.innerHTML = "";

		if (!files.length) {
			const opt = document.createElement("option");
			opt.value = "";
			opt.textContent = "Không tìm thấy file .cfg";
			select.appendChild(opt);
			msg.textContent = "Không tìm thấy file .cfg trong configFiles/.";
			return;
		}

		files.forEach((f) => {
			const opt = document.createElement("option");
			opt.value = f.relative || f.path;
			opt.textContent = f.name;
			select.appendChild(opt);
		});

		// default selection -> populate text field
		const cfgPath = document.getElementById("cfgPath");
		if (cfgPath && select.value) {
			cfgPath.value = select.value;
		}

		msg.textContent = `Đã tải ${files.length} file.`;
	} catch (err) {
		msg.textContent = `Tải thất bại: ${err.message}`;
	}
}

async function loadMetrics() {
	const path = String(document.getElementById("cfgPath").value || "").trim();
	const msg = document.getElementById("cfgMessage");
	const out = document.getElementById("metricsOut");
	msg.textContent = "Đang tính radar metrics...";

	try {
		const data = await fetchJson(`/config/metrics?path=${encodeURIComponent(path)}`);
		out.textContent = JSON.stringify(data.metrics || {}, null, 2);
		msg.textContent = `OK: ${data.cfg_path}`;
	} catch (err) {
		out.textContent = "{}";
		msg.textContent = `Tính metrics thất bại: ${err.message}`;
	}
}

function connectResultsSocket() {
	if (resultsSocket && resultsSocket.readyState === WebSocket.OPEN) return;
	const msg = document.getElementById("rtMessage");
	const url = `ws://${location.host}/realtime/ws/results?tail=50&interval=0.5`;
	resultsSocket = new WebSocket(url);

	resultsSocket.onopen = () => {
		msg.textContent = "Đã kết nối WebSocket kết quả.";
	};
	resultsSocket.onclose = () => {
		msg.textContent = "Mất kết nối WebSocket kết quả.";
	};
	resultsSocket.onerror = () => {
		msg.textContent = "Lỗi WebSocket kết quả.";
	};
	resultsSocket.onmessage = (evt) => {
		try {
			const payload = JSON.parse(evt.data);
			const results = payload.results || [];
			results.forEach((r) => appendResultLine(r));
		} catch {
			// ignore
		}
	};
}

async function startRealtime() {
	const com = String(document.getElementById("comPortSelect").value || "").trim();
	const cfg = String(document.getElementById("cfgPath").value || "").trim();
	const msg = document.getElementById("rtMessage");
	msg.textContent = "Đang bắt đầu hardware...";

	try {
		const data = await fetchJson("/realtime/start", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ com_port: com, cfg_path: cfg, cli_baud: 921600 })
		});
		setStatus(data.running ? "đang chạy" : "đã bắt đầu");
		msg.textContent = "Đã bắt đầu hardware.";
		connectResultsSocket();
	} catch (err) {
		msg.textContent = `Bắt đầu thất bại: ${err.message}`;
		setStatus("lỗi");
	}
}

async function stopRealtime() {
	const msg = document.getElementById("rtMessage");
	msg.textContent = "Đang dừng...";

	try {
		const data = await fetchJson("/realtime/stop", { method: "POST" });
		setStatus(data.running ? "đang chạy" : "đã dừng");
		msg.textContent = "Đã dừng.";
		if (resultsSocket) {
			resultsSocket.close();
			resultsSocket = null;
		}
	} catch (err) {
		msg.textContent = `Dừng thất bại: ${err.message}`;
	}
}

async function refreshRealtimeStatus() {
	const msg = document.getElementById("rtMessage");
	try {
		const data = await fetchJson("/realtime/status");
		setStatus(data.running ? "đang chạy" : "nhàn rỗi");
		msg.textContent = JSON.stringify(data, null, 2);
	} catch (err) {
		msg.textContent = `Lấy trạng thái thất bại: ${err.message}`;
	}
}

function wireUi() {
	document.getElementById("comRescan").addEventListener("click", loadComPorts);
	document.getElementById("cfgReload").addEventListener("click", loadCfgFiles);
	document.getElementById("cfgMetrics").addEventListener("click", loadMetrics);
	document.getElementById("rtStart").addEventListener("click", startRealtime);
	document.getElementById("rtStop").addEventListener("click", stopRealtime);
	document.getElementById("rtStatus").addEventListener("click", refreshRealtimeStatus);

	const cfgSelect = document.getElementById("cfgSelect");
	cfgSelect.addEventListener("change", () => {
		const cfgPath = document.getElementById("cfgPath");
		cfgPath.value = cfgSelect.value;
	});
}

(async () => {
	setStatus("nhàn rỗi");
	wireUi();
	await loadComPorts();
	await loadCfgFiles();
})();
