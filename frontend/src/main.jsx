import { Fragment, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const gql = async (query, variables) => {
  const r = await fetch("/graphql", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  const body = await r.json();
  if (body.errors) throw new Error(body.errors[0].message);
  return body.data;
};

const DASHBOARD = `{
  environment
  featureFlags { name enabled }
  stations { connectorId stationName powerKw status sagaId }
  wallets { fleetId balance }
  telemetry { sessionId connectorId status powerKw kwhDelivered }
  sagas { sagaId status connectorId fleetId amount steps createdAt }
}`;

function App() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [fleet, setFleet] = useState("fleet-rapiexpress");
  const [kwh, setKwh] = useState(40);
  const [open, setOpen] = useState(null);

  const refresh = () => gql(DASHBOARD).then(setD, (e) => setErr(e.message));
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000); // "tiempo real" por polling cada 2 s
    return () => clearInterval(t);
  }, []);

  const act = (q, v) => gql(q, v).then(refresh, (e) => setErr(e.message));
  const reserve = (connectorId) =>
    act(
      `mutation($c:String!,$f:String!,$k:Float!){ reserveAndCharge(connectorId:$c, fleetId:$f, kwh:$k){ sagaId } }`,
      { c: connectorId, f: fleet, k: Number(kwh) },
    );
  const release = (s) =>
    act(`mutation($s:String!,$c:String!){ releaseConnector(sagaId:$s, connectorId:$c) }`, {
      s: s.sagaId,
      c: s.connectorId,
    });
  const toggle = (f) =>
    act(`mutation($n:String!,$e:Boolean!){ setFeatureFlag(name:$n, enabled:$e){ enabled } }`, {
      n: f.name,
      e: !f.enabled,
    });

  if (!d) return <p className="pad">{err || "Cargando…"}</p>;
  return (
    <main>
      <header className={`env-${d.environment}`}>
        <h1>AegisVolt</h1>
        <span className="badge">ambiente: {d.environment}</span>
      </header>
      {err && <p className="error" onClick={() => setErr("")}>{err}</p>}

      <section>
        <h2>Feature Flags ({d.environment})</h2>
        <div className="row">
          {d.featureFlags.map((f) => (
            <label key={f.name} className={`flag ${f.enabled ? "on" : "off"}`}>
              <input type="checkbox" checked={f.enabled} onChange={() => toggle(f)} />
              {f.name}
            </label>
          ))}
        </div>
      </section>

      <section>
        <h2>Electrolineras y conectores</h2>
        <div className="row">
          <label>Flota <select value={fleet} onChange={(e) => setFleet(e.target.value)}>
            {d.wallets.map((w) => <option key={w.fleetId} value={w.fleetId}>{w.fleetId} (${w.balance.toFixed(2)})</option>)}
          </select></label>
          <label>kWh <input type="number" min="1" value={kwh} onChange={(e) => setKwh(e.target.value)} /></label>
        </div>
        <div className="grid">
          {d.stations.map((c) => {
            const t = d.telemetry.find((s) => s.sessionId === c.sagaId && s.status === "ACTIVE");
            return (
              <div key={c.connectorId} className={`card ${c.status}`}>
                <b>{c.connectorId}</b>
                <small>{c.stationName} · {c.powerKw} kW</small>
                <span className="status">{c.status}</span>
                {t && <small>⚡ {t.powerKw} kW · {t.kwhDelivered} kWh entregados</small>}
                {c.status === "AVAILABLE" && <button onClick={() => reserve(c.connectorId)}>Reservar y activar carga</button>}
                {c.status === "CHARGING" && <button onClick={() => release(c)}>Terminar carga</button>}
              </div>
            );
          })}
        </div>
      </section>

      <section>
        <h2>Historial de transacciones SAGA</h2>
        <table>
          <thead><tr><th>Saga / correlation_id</th><th>Conector</th><th>Flota</th><th>Monto</th><th>Estado</th></tr></thead>
          <tbody>
            {d.sagas.map((s) => (
              <Fragment key={s.sagaId}>
                <tr onClick={() => setOpen(open === s.sagaId ? null : s.sagaId)} className="click">
                  <td><code>{s.sagaId}</code></td><td>{s.connectorId}</td><td>{s.fleetId}</td>
                  <td>{s.amount ?? "-"}</td><td className={`st ${s.status}`}>{s.status}</td>
                </tr>
                {open === s.sagaId && (
                  <tr><td colSpan="5">
                    <ol>{s.steps.map((p, i) => (
                      <li key={i} className={p.ok ? "ok" : "fail"}>
                        {p.step} {p.ok ? "✔" : "✘"} {p.detail} <small>{new Date(p.at * 1000).toLocaleTimeString()}</small>
                      </li>))}
                    </ol>
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<App />);
