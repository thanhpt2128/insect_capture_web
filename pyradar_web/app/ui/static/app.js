let resultsSocket = null;
let lastRangePlot = null;
let lastRangeSeq = null;

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

	const copy = JSON.parse(JSON.stringify(obj || {}));
	const data = copy.data || copy;
	if (data.range_plot) {
		const range = data.range_plot;
		data.range_plot = {
			ready: range.ready,
			frame_count: range.frame_count,
			range_bins: range.range_bins,
			fft_size: range.fft_size,
			min_db: range.min_db,
			max_db: range.max_db,
			range_resolution_m: range.range_resolution_m,
			error: range.error
		};
	}

	const line = JSON.stringify(copy, null, 2);
	out.textContent = (out.textContent ? out.textContent + "\n\n" : "") + line;
	const maxChars = 30000;
	if (out.textContent.length > maxChars) {
		out.textContent = out.textContent.slice(out.textContent.length - maxChars);
	}
	out.scrollTop = out.scrollHeight;
}

function resizeCanvas(canvas) {
	const ratio = window.devicePixelRatio || 1;
	const rect = canvas.getBoundingClientRect();
	const width = Math.max(1, Math.floor(rect.width * ratio));
	const height = Math.max(1, Math.floor(rect.height * ratio));
	if (canvas.width !== width || canvas.height !== height) {
		canvas.width = width;
		canvas.height = height;
	}
	return { width, height, ratio };
}

function colorRamp(t) {
	const x = Math.max(0, Math.min(1, t));
	const stops = [
		[15, 23, 42],
		[30, 64, 175],
		[8, 145, 178],
		[34, 197, 94],
		[250, 204, 21]
	];
	const scaled = x * (stops.length - 1);
	const i = Math.min(stops.length - 2, Math.floor(scaled));
	const local = scaled - i;
	const a = stops[i];
	const b = stops[i + 1];
	return [
		Math.round(a[0] + (b[0] - a[0]) * local),
		Math.round(a[1] + (b[1] - a[1]) * local),
		Math.round(a[2] + (b[2] - a[2]) * local)
	];
}

function drawEmptyCanvas(canvas, label) {
	if (!canvas) return;
	const { width, height, ratio } = resizeCanvas(canvas);
	const ctx = canvas.getContext("2d");
	ctx.clearRect(0, 0, width, height);
	ctx.fillStyle = "#101827";
	ctx.fillRect(0, 0, width, height);
	ctx.fillStyle = "#9ca3af";
	ctx.font = `${12 * ratio}px system-ui, sans-serif`;
	ctx.textAlign = "center";
	ctx.fillText(label, width / 2, height / 2);
}

function drawRangeProfile(profile, minDb, maxDb) {
	const canvas = document.getElementById("rangeProfileCanvas");
	if (!canvas || !Array.isArray(profile) || !profile.length) {
		drawEmptyCanvas(canvas, "No profile data");
		return;
	}

	const { width, height, ratio } = resizeCanvas(canvas);
	const ctx = canvas.getContext("2d");
	const padLeft = 44 * ratio;
	const padRight = 16 * ratio;
	const padTop = 16 * ratio;
	const padBottom = 28 * ratio;
	const plotW = Math.max(1, width - padLeft - padRight);
	const plotH = Math.max(1, height - padTop - padBottom);
	let min = Number.isFinite(minDb) ? minDb : Math.min(...profile);
	let max = Number.isFinite(maxDb) ? maxDb : Math.max(...profile);
	if (min === max) {
		min -= 1;
		max += 1;
	}

	ctx.clearRect(0, 0, width, height);
	ctx.fillStyle = "#101827";
	ctx.fillRect(0, 0, width, height);
	ctx.strokeStyle = "#233047";
	ctx.lineWidth = 1 * ratio;
	for (let i = 0; i <= 4; i += 1) {
		const y = padTop + (plotH * i) / 4;
		ctx.beginPath();
		ctx.moveTo(padLeft, y);
		ctx.lineTo(width - padRight, y);
		ctx.stroke();
	}

	ctx.strokeStyle = "#38bdf8";
	ctx.lineWidth = 2 * ratio;
	ctx.beginPath();
	profile.forEach((value, index) => {
		const x = padLeft + (plotW * index) / Math.max(1, profile.length - 1);
		const y = padTop + plotH - ((value - min) / (max - min)) * plotH;
		if (index === 0) ctx.moveTo(x, y);
		else ctx.lineTo(x, y);
	});
	ctx.stroke();

	ctx.fillStyle = "#cbd5e1";
	ctx.font = `${11 * ratio}px system-ui, sans-serif`;
	ctx.textAlign = "left";
	ctx.fillText(`${max.toFixed(1)} dB`, 8 * ratio, padTop + 8 * ratio);
	ctx.fillText(`${min.toFixed(1)} dB`, 8 * ratio, padTop + plotH);
	ctx.textAlign = "center";
	ctx.fillText("Range bin", padLeft + plotW / 2, height - 8 * ratio);
}

function drawRangeTime(matrix, minDb, maxDb) {
	const canvas = document.getElementById("rangeTimeCanvas");
	if (!canvas || !Array.isArray(matrix) || !matrix.length || !Array.isArray(matrix[0])) {
		drawEmptyCanvas(canvas, "No range-time data");
		return;
	}

	const { width, height, ratio } = resizeCanvas(canvas);
	const ctx = canvas.getContext("2d");
	const padLeft = 44 * ratio;
	const padRight = 16 * ratio;
	const padTop = 12 * ratio;
	const padBottom = 28 * ratio;
	const plotW = Math.max(1, width - padLeft - padRight);
	const plotH = Math.max(1, height - padTop - padBottom);
	const rows = matrix.length;
	const cols = matrix[0].length;
	let min = Number.isFinite(minDb) ? minDb : Infinity;
	let max = Number.isFinite(maxDb) ? maxDb : -Infinity;
	if (!Number.isFinite(min) || !Number.isFinite(max)) {
		matrix.forEach((row) => {
			row.forEach((value) => {
				if (Number.isFinite(value)) {
					min = Math.min(min, value);
					max = Math.max(max, value);
				}
			});
		});
	}
	if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) {
		min = 0;
		max = 1;
	}

	ctx.clearRect(0, 0, width, height);
	ctx.fillStyle = "#101827";
	ctx.fillRect(0, 0, width, height);

	const cellW = plotW / Math.max(1, rows);
	const cellH = plotH / Math.max(1, cols);
	for (let frame = 0; frame < rows; frame += 1) {
		const row = matrix[frame];
		for (let bin = 0; bin < cols; bin += 1) {
			const value = row[bin];
			const rgb = colorRamp((value - min) / (max - min));
			ctx.fillStyle = `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
			const x = padLeft + frame * cellW;
			const y = padTop + plotH - (bin + 1) * cellH;
			ctx.fillRect(x, y, Math.ceil(cellW), Math.ceil(cellH));
		}
	}

	ctx.strokeStyle = "#d5dae4";
	ctx.lineWidth = 1 * ratio;
	ctx.strokeRect(padLeft, padTop, plotW, plotH);
	ctx.fillStyle = "#cbd5e1";
	ctx.font = `${11 * ratio}px system-ui, sans-serif`;
	ctx.textAlign = "center";
	ctx.fillText("Frame in batch", padLeft + plotW / 2, height - 8 * ratio);
	ctx.save();
	ctx.translate(14 * ratio, padTop + plotH / 2);
	ctx.rotate(-Math.PI / 2);
	ctx.fillText("Range bin", 0, 0);
	ctx.restore();
}

function renderRangePlot(rangePlot, seq) {
	const meta = document.getElementById("rangeMeta");
	if (!rangePlot) return;
	if (!rangePlot.ready) {
		if (meta) meta.textContent = rangePlot.error || "Range plot is not ready.";
		return;
	}

	lastRangePlot = rangePlot;
	lastRangeSeq = seq;
	drawRangeProfile(rangePlot.range_profile || [], rangePlot.min_db, rangePlot.max_db);
	drawRangeTime(rangePlot.range_time || [], rangePlot.min_db, rangePlot.max_db);

	if (meta) {
		const resolution = rangePlot.range_resolution_m
			? `, ${rangePlot.range_resolution_m} m/bin`
			: "";
		meta.textContent = `Seq ${seq}: ${rangePlot.frame_count} frames x ${rangePlot.range_bins} bins${resolution}`;
	}
}

function handleResultItem(item) {
	const data = item && item.data ? item.data : item;
	if (data && data.range_plot) {
		renderRangePlot(data.range_plot, data.seq);
	}
	appendResultLine(item);
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
			results.forEach((r) => handleResultItem(r));
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
			body: JSON.stringify({ com_port: com, cfg_path: cfg, cli_baud: 921600, numframes: 30 })
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
	setStatus("Idle");
	wireUi();
	drawEmptyCanvas(document.getElementById("rangeProfileCanvas"), "Waiting for range profile");
	drawEmptyCanvas(document.getElementById("rangeTimeCanvas"), "Waiting for range-time");
	window.addEventListener("resize", () => {
		if (lastRangePlot) {
			renderRangePlot(lastRangePlot, lastRangeSeq);
		} else {
			drawEmptyCanvas(document.getElementById("rangeProfileCanvas"), "Waiting for range profile");
			drawEmptyCanvas(document.getElementById("rangeTimeCanvas"), "Waiting for range-time");
		}
	});
	await loadComPorts();
	await loadCfgFiles();
})();
