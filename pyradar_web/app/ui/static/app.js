let resultsSocket = null;
let lastRangePlot = null;
let lastRangeSeq = null;
let lastResultMarker = null;
let rangeProfileChart = null;
let lastRangeStageRank = -1;
let lastResultSeq = null;
let lastResultStageRank = -1;

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

function clearResultLine() {
	const out = document.getElementById("resultOut");
	if (!out) return;
	out.textContent = "";
	out.scrollTop = 0;
}

function setLatestResultLine(obj) {
	const out = document.getElementById("resultOut");
	if (!out) return;

	const data = obj && obj.data ? obj.data : obj || {};
	const capture = data.capture || {};
	const range = data.range_plot || {};
	const stage = normalizeStage(data.stage);
	const frameCount = Number.isFinite(range.frame_count) ? range.frame_count : capture.numframes;
	const bins = Number.isFinite(range.range_bins) ? range.range_bins : null;
	const dbRange = Number.isFinite(range.min_db) && Number.isFinite(range.max_db)
		? `${range.min_db.toFixed(1)}..${range.max_db.toFixed(1)} dB`
		: "n/a";
	const result = data.result || {};
	const probaStr = result.proba && typeof result.proba === "object"
		? Object.entries(result.proba)
			.map(([k, v]) => `${k}:${Number(v).toFixed(2)}`)
			.join(",")
		: null;
	const parts = [
		`seq=${data.seq ?? "-"}`,
		`stage=${stage}`,
		`type=${data.type || "-"}`,
		`frames=${frameCount ?? "-"}`,
		`bins=${bins ?? "-"}`,
		`size=${capture.size ?? "-"}`,
		`range=${dbRange}`,
		`result=${result.label ? result.label : "-"}`
	];
	if (typeof data.is_insect === "boolean") parts.push(`insect=${data.is_insect}`);
	if (Number.isFinite(data.power_threshold)) parts.push(`power=${data.power_threshold.toFixed(1)}`);
	if (Number.isFinite(result.score)) parts.push(`score=${result.score.toFixed(3)}`);
	if (probaStr) parts.push(`proba=[${probaStr}]`);
	out.textContent = parts.join(" | ");
	out.scrollTop = out.scrollHeight;
}

function ensureCharts() {
	if (typeof window.echarts === "undefined") {
		return false;
	}

	if (!rangeProfileChart) {
		const el = document.getElementById("rangeProfilePlot");
		if (el) {
			rangeProfileChart = window.echarts.init(el, null, { renderer: "canvas" });
		}
	}

	return Boolean(rangeProfileChart);
}

function baseChartOption(titleText) {
	return {
		animation: false,
		backgroundColor: "#101827",
		title: {
			text: titleText,
			left: "center",
			top: "middle",
			textStyle: {
				color: "#9ca3af",
				fontSize: 12,
				fontWeight: "normal"
			}
		}
	};
}

function showChartPlaceholder(chart, label) {
	if (!chart) return;
	chart.clear();
	chart.setOption(baseChartOption(label), { notMerge: true, lazyUpdate: true, silent: true });
}

function resizeHeatmapCanvas() {
	const canvas = document.getElementById("rangeTimePlot");
	if (!canvas) return null;
	const ratio = window.devicePixelRatio || 1;
	const width = Math.max(1, Math.floor(canvas.clientWidth * ratio));
	const height = Math.max(1, Math.floor(canvas.clientHeight * ratio));
	if (canvas.width !== width || canvas.height !== height) {
		canvas.width = width;
		canvas.height = height;
	}
	return { canvas, width, height, ratio };
}

function drawHeatmapPlaceholder(label) {
	const sized = resizeHeatmapCanvas();
	if (!sized) return;
	const { canvas, width, height, ratio } = sized;
	const ctx = canvas.getContext("2d");
	ctx.clearRect(0, 0, width, height);
	ctx.fillStyle = "#101827";
	ctx.fillRect(0, 0, width, height);
	ctx.fillStyle = "#9ca3af";
	ctx.font = `${12 * ratio}px system-ui, sans-serif`;
	ctx.textAlign = "center";
	ctx.textBaseline = "middle";
	ctx.fillText(label, width / 2, height / 2);
}

function resetRangeView() {
	lastRangePlot = null;
	lastRangeSeq = null;
	lastResultMarker = null;
	lastRangeStageRank = -1;
	lastResultSeq = null;
	lastResultStageRank = -1;
	clearResultLine();
	if (!ensureCharts()) {
		return;
	}
	showChartPlaceholder(rangeProfileChart, "Waiting for range profile");
	drawHeatmapPlaceholder("Waiting for range-time");
}

function normalizeStage(stage) {
	if (stage === "inference") {
		return "inference";
	}
	return "preprocess";
}

function stageRank(stage) {
	return normalizeStage(stage) === "inference" ? 1 : 0;
}

function shouldAcceptSeq(seq, currentSeq, rank, currentRank) {
	if (!Number.isFinite(seq)) {
		return true;
	}
	if (!Number.isFinite(currentSeq)) {
		return true;
	}
	if (seq > currentSeq) {
		return true;
	}
	if (seq === currentSeq && rank >= currentRank) {
		return true;
	}
	return false;
}

function computePlotScale(rangePlot, profile, matrix) {
	let min = Number.isFinite(rangePlot?.min_db) ? Number(rangePlot.min_db) : Infinity;
	let max = Number.isFinite(rangePlot?.max_db) ? Number(rangePlot.max_db) : -Infinity;

	if (!Number.isFinite(min) || !Number.isFinite(max)) {
		const values = [];
		if (Array.isArray(profile)) {
			values.push(...profile.filter(Number.isFinite));
		}
		if (Array.isArray(matrix)) {
			matrix.forEach((row) => {
				if (Array.isArray(row)) {
					values.push(...row.filter(Number.isFinite));
				}
			});
		}
		if (values.length) {
			min = Math.min(...values);
			max = Math.max(...values);
		} else {
			min = 0;
			max = 1;
		}
	}

	if (min === max) {
		min -= 1;
		max += 1;
	}

	return { min, max };
}

function computeHeatmapScale(matrix, fallbackScale) {
	const values = [];
	if (Array.isArray(matrix)) {
		matrix.forEach((row) => {
			if (Array.isArray(row)) {
				row.forEach((value) => {
					if (Number.isFinite(value)) {
						values.push(value);
					}
				});
			}
		});
	}

	if (values.length < 8) {
		return fallbackScale;
	}

	values.sort((a, b) => a - b);
	const lowIndex = Math.floor(values.length * 0.08);
	const highIndex = Math.floor(values.length * 0.92);
	let min = values[Math.max(0, Math.min(values.length - 1, lowIndex))];
	let max = values[Math.max(0, Math.min(values.length - 1, highIndex))];
	if (min === max) {
		min = fallbackScale.min;
		max = fallbackScale.max;
	}
	return { min, max };
}

function heatmapColors() {
	return [
		[0.0, "#16324f"],
		[0.18, "#1f6f8b"],
		[0.36, "#38bdf8"],
		[0.58, "#67e8f9"],
		[0.78, "#fde047"],
		[1.0, "#fff7cc"]
	];
}

function drawRangeProfile(profile, scale) {
	if (!ensureCharts()) {
		return;
	}
	if (!Array.isArray(profile) || !profile.length) {
		showChartPlaceholder(rangeProfileChart, "No profile data");
		return;
	}

	rangeProfileChart.setOption(
		{
			animation: false,
			backgroundColor: "#101827",
			grid: { left: 52, right: 16, top: 16, bottom: 36 },
			xAxis: {
				type: "category",
				data: profile.map((_value, index) => index),
				name: "Range bin",
				nameLocation: "middle",
				nameGap: 28,
				axisLine: { lineStyle: { color: "#d5dae4" } },
				axisLabel: { color: "#cbd5e1", showMaxLabel: true, showMinLabel: true },
				splitLine: { show: false }
			},
			yAxis: {
				type: "value",
				min: scale.min,
				max: scale.max,
				name: "Amplitude (dB)",
				nameTextStyle: { color: "#cbd5e1" },
				axisLine: { lineStyle: { color: "#d5dae4" } },
				axisLabel: { color: "#cbd5e1" },
				splitLine: { lineStyle: { color: "#233047" } }
			},
			tooltip: {
				trigger: "axis",
				borderWidth: 0,
				backgroundColor: "rgba(17,24,39,0.92)",
				textStyle: { color: "#e5e7eb" }
			},
			series: [
				{
					type: "line",
					data: profile,
					showSymbol: false,
					smooth: false,
					lineStyle: { color: "#38bdf8", width: 2 }
				}
			]
		},
		{ notMerge: true, lazyUpdate: true, silent: true }
	);
}

function drawRangeTime(matrix, scale) {
	if (!Array.isArray(matrix) || !matrix.length || !Array.isArray(matrix[0])) {
		drawHeatmapPlaceholder("No range-time data");
		return;
	}

	const sized = resizeHeatmapCanvas();
	if (!sized) return;
	const { canvas, width, height, ratio } = sized;
	const ctx = canvas.getContext("2d");
	const frames = matrix.length;
	const bins = matrix[0].length;
	const padLeft = 46 * ratio;
	const padRight = 18 * ratio;
	const padTop = 12 * ratio;
	const padBottom = 28 * ratio;
	const plotW = Math.max(1, width - padLeft - padRight);
	const plotH = Math.max(1, height - padTop - padBottom);
	const image = ctx.createImageData(frames, bins);
	const span = Math.max(1e-6, scale.max - scale.min);

	for (let frame = 0; frame < frames; frame += 1) {
		const row = matrix[frame];
		for (let bin = 0; bin < bins; bin += 1) {
			const value = Number.isFinite(row[bin]) ? row[bin] : scale.min;
			const normalized = Math.max(0, Math.min(1, (value - scale.min) / span));
			const palette = heatmapColors();
			let left = palette[0];
			let right = palette[palette.length - 1];
			for (let index = 0; index < palette.length - 1; index += 1) {
				if (normalized >= palette[index][0] && normalized <= palette[index + 1][0]) {
					left = palette[index];
					right = palette[index + 1];
					break;
				}
			}
			const localSpan = Math.max(1e-6, right[0] - left[0]);
			const t = (normalized - left[0]) / localSpan;
			const leftRgb = left[1].match(/[0-9a-f]{2}/gi).map((hex) => Number.parseInt(hex, 16));
			const rightRgb = right[1].match(/[0-9a-f]{2}/gi).map((hex) => Number.parseInt(hex, 16));
			const rgb = [
				Math.round(leftRgb[0] + (rightRgb[0] - leftRgb[0]) * t),
				Math.round(leftRgb[1] + (rightRgb[1] - leftRgb[1]) * t),
				Math.round(leftRgb[2] + (rightRgb[2] - leftRgb[2]) * t)
			];
			const y = bins - 1 - bin;
			const offset = (y * frames + frame) * 4;
			image.data[offset] = rgb[0];
			image.data[offset + 1] = rgb[1];
			image.data[offset + 2] = rgb[2];
			image.data[offset + 3] = 255;
		}
	}

	const buffer = document.createElement("canvas");
	buffer.width = frames;
	buffer.height = bins;
	buffer.getContext("2d").putImageData(image, 0, 0);

	ctx.clearRect(0, 0, width, height);
	ctx.fillStyle = "#101827";
	ctx.fillRect(0, 0, width, height);
	ctx.imageSmoothingEnabled = false;
	ctx.drawImage(buffer, padLeft, padTop, plotW, plotH);

	ctx.strokeStyle = "#d5dae4";
	ctx.lineWidth = Math.max(1, ratio);
	ctx.strokeRect(padLeft, padTop, plotW, plotH);

	ctx.fillStyle = "#cbd5e1";
	ctx.font = `${11 * ratio}px system-ui, sans-serif`;
	ctx.textAlign = "center";
	ctx.textBaseline = "middle";
	ctx.fillText("Frame in batch", padLeft + plotW / 2, height - 10 * ratio);
	ctx.save();
	ctx.translate(14 * ratio, padTop + plotH / 2);
	ctx.rotate(-Math.PI / 2);
	ctx.fillText("Range bin", 0, 0);
	ctx.restore();

	ctx.textAlign = "left";
	ctx.fillText(`${scale.max.toFixed(1)} dB`, width - 74 * ratio, padTop + 8 * ratio);
	ctx.fillText(`${scale.min.toFixed(1)} dB`, width - 74 * ratio, padTop + plotH - 4 * ratio);
}

function drawCurrentRangeView(seq) {
	const meta = document.getElementById("rangeMeta");
	const rangePlot = lastRangePlot;
	if (!rangePlot || !rangePlot.ready) {
		return;
	}

	const profile = Array.isArray(rangePlot.range_profile) ? rangePlot.range_profile : [];
	const matrix = Array.isArray(rangePlot.range_time) ? rangePlot.range_time : [];
	const scale = computePlotScale(rangePlot, profile, matrix);
	const heatmapScale = computeHeatmapScale(matrix, scale);

	drawRangeProfile(profile, scale);
	drawRangeTime(matrix, heatmapScale);

	if (meta) {
		const resolution = rangePlot.range_resolution_m ? `, ${rangePlot.range_resolution_m} m/bin` : "";
		meta.textContent =
			`Seq ${seq}: ${rangePlot.frame_count} frames in batch, ` +
			`${matrix.length || rangePlot.frame_count} visible frames x ${rangePlot.range_bins} bins${resolution}`;
	}
}

function ingestRangePlot(rangePlot, seq, stage) {
	const meta = document.getElementById("rangeMeta");
	if (!rangePlot) return;
	if (!rangePlot.ready) {
		if (meta) meta.textContent = rangePlot.error || "Range plot is not ready.";
		return;
	}

	const seqNumber = Number(seq);
	const rank = stageRank(stage);
	if (!shouldAcceptSeq(seqNumber, lastRangeSeq, rank, lastRangeStageRank)) {
		return;
	}

	lastRangePlot = rangePlot;
	lastRangeSeq = seqNumber;
	lastRangeStageRank = rank;
	drawCurrentRangeView(seqNumber);
}

function handleResultItem(item) {
	const marker = item ? `${item.ts ?? ""}:${item.id ?? ""}` : null;
	if (marker && marker === lastResultMarker) {
		return;
	}

	lastResultMarker = marker;
	const data = item && item.data ? item.data : item;
	if (data?.type === "status") {
		return;
	}
	const seqNumber = Number(data?.seq);
	const stage = normalizeStage(data?.stage);
	const rank = stageRank(stage);
	if (data && data.range_plot) {
		ingestRangePlot(data.range_plot, seqNumber, stage);
	}
	if (shouldAcceptSeq(seqNumber, lastResultSeq, rank, lastResultStageRank)) {
		lastResultSeq = seqNumber;
		lastResultStageRank = rank;
		setLatestResultLine(item);
	}
}

async function loadComPorts() {
	const select = document.getElementById("comPortSelect");
	const msg = document.getElementById("comMessage");
	msg.textContent = "Scanning...";

	try {
		const data = await fetchJson("/com/list");
		const ports = data.ports || [];
		select.innerHTML = "";

		if (!ports.length) {
			const opt = document.createElement("option");
			opt.value = "";
			opt.textContent = "No COM ports found";
			select.appendChild(opt);
			msg.textContent = "No COM ports found.";
			return;
		}

		ports.forEach((port) => {
			const opt = document.createElement("option");
			opt.value = port.device;
			opt.textContent = port.description ? `${port.device} - ${port.description}` : port.device;
			select.appendChild(opt);
		});

		msg.textContent = `Found ${ports.length} ports.`;
	} catch (err) {
		msg.textContent = `Scan failed: ${err.message}`;
	}
}

async function loadCfgFiles() {
	const select = document.getElementById("cfgSelect");
	const msg = document.getElementById("cfgMessage");
	msg.textContent = "Loading cfg files...";

	try {
		const data = await fetchJson("/config/list");
		const files = data.files || [];
		select.innerHTML = "";

		if (!files.length) {
			const opt = document.createElement("option");
			opt.value = "";
			opt.textContent = "No .cfg files found";
			select.appendChild(opt);
			msg.textContent = "No .cfg files found in configFiles/.";
			return;
		}

		files.forEach((file) => {
			const opt = document.createElement("option");
			opt.value = file.relative || file.path;
			opt.textContent = file.name;
			select.appendChild(opt);
		});

		const cfgPath = document.getElementById("cfgPath");
		if (cfgPath && select.value) {
			cfgPath.value = select.value;
		}

		msg.textContent = `Loaded ${files.length} files.`;
	} catch (err) {
		msg.textContent = `Load failed: ${err.message}`;
	}
}

async function loadMetrics() {
	const path = String(document.getElementById("cfgPath").value || "").trim();
	const msg = document.getElementById("cfgMessage");
	const out = document.getElementById("metricsOut");
	msg.textContent = "Calculating radar metrics...";

	try {
		const data = await fetchJson(`/config/metrics?path=${encodeURIComponent(path)}`);
		out.textContent = JSON.stringify(data.metrics || {}, null, 2);
		msg.textContent = `OK: ${data.cfg_path}`;
	} catch (err) {
		out.textContent = "{}";
		msg.textContent = `Metrics failed: ${err.message}`;
	}
}

function connectResultsSocket() {
	if (resultsSocket) {
		if (
			resultsSocket.readyState === WebSocket.OPEN ||
			resultsSocket.readyState === WebSocket.CONNECTING
		) {
			return;
		}
		resultsSocket.close();
		resultsSocket = null;
	}
	const msg = document.getElementById("rtMessage");
	const url = `ws://${location.host}/realtime/ws/results`;
	resultsSocket = new WebSocket(url);

	resultsSocket.onopen = () => {
		msg.textContent = "Result WebSocket connected.";
	};
	resultsSocket.onclose = () => {
		msg.textContent = "Result WebSocket disconnected.";
	};
	resultsSocket.onerror = () => {
		msg.textContent = "Result WebSocket error.";
	};
	resultsSocket.onmessage = (evt) => {
		try {
			const payload = JSON.parse(evt.data);
			const results = payload.results || [];
			if (results.length) {
				handleResultItem(results[results.length - 1]);
			}
		} catch {
			// ignore
		}
	};
}

async function startRealtime() {
	const com = String(document.getElementById("comPortSelect").value || "").trim();
	const cfg = String(document.getElementById("cfgPath").value || "").trim();
	const msg = document.getElementById("rtMessage");
	msg.textContent = "Starting hardware...";
	lastResultMarker = null;
	clearResultLine();

	try {
		const data = await fetchJson("/realtime/start", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ com_port: com, cfg_path: cfg, cli_baud: 921600, numframes: 30 })
		});
		resetRangeView();
		setStatus(data.running ? "running" : "started");
		msg.textContent = "Hardware started.";
		connectResultsSocket();
	} catch (err) {
		msg.textContent = `Start failed: ${err.message}`;
		setStatus("error");
	}
}

async function stopRealtime() {
	const msg = document.getElementById("rtMessage");
	msg.textContent = "Stopping...";

	try {
		const data = await fetchJson("/realtime/stop", { method: "POST" });
		setStatus(data.running ? "running" : "stopped");
		msg.textContent = "Stopped.";
		if (resultsSocket) {
			resultsSocket.close();
			resultsSocket = null;
		}
		resetRangeView();
	} catch (err) {
		msg.textContent = `Stop failed: ${err.message}`;
	}
}

async function refreshRealtimeStatus() {
	const msg = document.getElementById("rtMessage");
	try {
		const data = await fetchJson("/realtime/status");
		setStatus(data.running ? "running" : "idle");
		msg.textContent = JSON.stringify(data, null, 2);
	} catch (err) {
		msg.textContent = `Status failed: ${err.message}`;
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
	resetRangeView();
	window.addEventListener("resize", () => {
		if (rangeProfileChart) {
			rangeProfileChart.resize();
		}
		if (lastRangePlot) {
			drawCurrentRangeView(lastRangeSeq);
		} else {
			drawHeatmapPlaceholder("Waiting for range-time");
		}
	});
	await loadComPorts();
	await loadCfgFiles();
})();
