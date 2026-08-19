import React, { useState, useEffect, useCallback } from 'react';
import { ArrowsClockwise, ChartLineUp } from '@phosphor-icons/react';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const MODES = [
  { key: 'all', label: 'Alle' },
  { key: 'live', label: 'Live-Logik' },
  { key: 'collection', label: 'Sammel' },
];
const DAY_OPTIONS = [
  { value: 0, label: 'Gesamt' },
  { value: 7, label: '7 Tage' },
  { value: 30, label: '30 Tage' },
  { value: 90, label: '90 Tage' },
];

const money = (v) => `${(v ?? 0) >= 0 ? '+' : ''}${(v ?? 0).toFixed(2)} $`;

const EquityCurveSvg = ({ points, height = 150 }) => {
  const w = 640, padX = 8, padY = 10;
  const ys = points.map(p => p.equity);
  const minY = Math.min(0, ...ys);
  const maxY = Math.max(0, ...ys);
  const spanY = (maxY - minY) || 1;
  const stepX = (w - padX * 2) / Math.max(1, points.length - 1);
  const coords = points.map((p, i) => {
    const x = padX + i * stepX;
    const y = padY + (1 - (p.equity - minY) / spanY) * (height - padY * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const zeroY = padY + (1 - (0 - minY) / spanY) * (height - padY * 2);
  const positive = points[points.length - 1].equity >= 0;
  return (
    <svg viewBox={`0 0 ${w} ${height}`} className="ai-equity-svg" preserveAspectRatio="none"
      style={{ width: '100%', height, display: 'block' }} data-testid="ai-equity-chart">
      <line x1={padX} x2={w - padX} y1={zeroY} y2={zeroY} stroke="#2A2D3A" strokeDasharray="3,3" />
      <polyline fill="none" stroke={positive ? '#00FF66' : '#FF3366'} strokeWidth="1.6" points={coords} />
    </svg>
  );
};

export const AIEquityPanel = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState('all');
  const [days, setDays] = useState(0);

  const load = useCallback(async (m = mode, d = days) => {
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/equity-curve?days=${d}&mode=${m}`).then(r => r.json());
      setData(res && typeof res === 'object' ? res : null);
    } catch (e) { setData(null); }
    setLoading(false);
  }, [mode, days]);

  useEffect(() => { load(mode, days); }, [mode, days, load]);

  const s = data?.summary;
  const points = data?.points || [];
  const last = points[points.length - 1];

  return (
    <div className="ai-learn-panel" data-testid="ai-equity-panel">
      <div className="ai-learn-title" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <ChartLineUp size={14} weight="fill" /> Verlauf – Equity-Kurve der KI-Trades
        <span style={{ display: 'inline-flex', gap: 4, marginLeft: 'auto', alignItems: 'center' }}>
          {MODES.map(m => (
            <button key={m.key}
              className={`ai-action-btn ${mode === m.key ? 'active' : ''}`}
              style={mode === m.key ? { opacity: 1 } : { opacity: 0.6 }}
              onClick={() => setMode(m.key)}
              data-testid={`ai-equity-mode-${m.key}`}>{m.label}</button>
          ))}
          <select value={days} onChange={e => setDays(Number(e.target.value))} data-testid="ai-equity-days-select">
            {DAY_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <button className="ai-action-btn" onClick={() => load()} title="Neu laden" data-testid="ai-equity-reload-btn">
            <ArrowsClockwise size={13} weight="bold" className={loading ? 'spin' : ''} />
          </button>
        </span>
      </div>
      <div style={{ fontSize: 11, opacity: 0.55, margin: '2px 0 6px' }}>
        Kumulierter realisierter PnL je geschlossenem KI-Trade (zeitlich sortiert) – steigt die Kurve
        über die Zeit, wird der KI Trader besser. „Live-Logik" = ohne Sammel-Trades.
      </div>
      {loading && !data && <div style={{ fontSize: 12, opacity: 0.7 }}>Lade…</div>}
      {!loading && points.length === 0 && (
        <div style={{ fontSize: 12, opacity: 0.7 }} data-testid="ai-equity-empty">
          Noch keine geschlossenen KI-Trades im gewählten Zeitraum – die Kurve erscheint,
          sobald Trades geschlossen wurden (nach einem Reset startet sie neu bei 0).
        </div>
      )}
      {points.length > 0 && (
        <>
          <EquityCurveSvg points={points} />
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, marginTop: 6 }}>
            <span data-testid="ai-equity-kpi-pnl" style={{ color: (s?.total_pnl ?? 0) >= 0 ? '#00FF66' : '#FF3366' }}>
              <b>PnL:</b> {money(s?.total_pnl)}
            </span>
            <span data-testid="ai-equity-kpi-trades"><b>Trades:</b> {s?.trades}</span>
            <span data-testid="ai-equity-kpi-winrate"><b>Winrate:</b> {s?.winrate}%</span>
            <span data-testid="ai-equity-kpi-dd"><b>Max Drawdown:</b> {(s?.max_drawdown ?? 0).toFixed(2)} $</span>
            <span data-testid="ai-equity-kpi-fees"><b>Fees:</b> {(s?.fees ?? 0).toFixed(2)} $</span>
            {last && <span style={{ opacity: 0.6 }}>Letzter Trade: {String(last.ts || '').slice(0, 16).replace('T', ' ')}</span>}
          </div>
        </>
      )}
    </div>
  );
};

export default AIEquityPanel;
