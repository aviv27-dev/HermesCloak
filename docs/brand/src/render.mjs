// Renders every brand asset and README image from the sources in this folder.
//   python3 docs/brand/src/fetch_fonts.py   (once: caches the web fonts locally)
//   node docs/brand/src/render.mjs   (needs `playwright`; a global install works with NODE_PATH=$(npm root -g);
//                                     CHROMIUM_PATH=/path/to/chromium to use an existing browser)
// All example data is synthetic; the masked outputs are what the engine really produces for it.
import { createRequire } from "node:module";
import { readFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright");   // CJS resolution honours NODE_PATH
const here = dirname(fileURLToPath(import.meta.url));
const brand = join(here, "..");
const images = join(brand, "..", "images");
mkdirSync(images, { recursive: true });
const mark = readFileSync(join(brand, "mark.svg"), "utf8");
const terminal = readFileSync(join(here, "verify-test.txt"), "utf8");

const C = { ink: "#0F172A", slate: "#475569", mute: "#94A3B8", line: "#E2E8F0", bg: "#F7F7F5",
            teal: "#0F766E", teal2: "#14B8A6", tealbg: "#CCFBF1", amber: "#B45309", amberbg: "#FEF3C7",
            night: "#0B1220", card: "#FFFFFF" };

const fontCache = join(here, ".fonts", "fonts.css");
if (!existsSync(fontCache)) throw new Error("run docs/brand/src/fetch_fonts.py first");
const fonts = `<style>${readFileSync(fontCache, "utf8")}</style>`;
const base = `
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:Inter,Heebo,sans-serif;color:${C.ink};-webkit-font-smoothing:antialiased}
  .mono{font-family:'JetBrains Mono',Heebo,'Noto Sans Math',monospace}
  .tok{background:${C.tealbg};color:${C.teal};border-radius:6px;padding:1px 6px;font-weight:600;
       font-family:'JetBrains Mono',Heebo,'Noto Sans Math',monospace;white-space:nowrap}
  .real{background:${C.amberbg};color:${C.amber};border-radius:6px;padding:1px 6px;font-weight:600;white-space:nowrap}
  .wm{font-family:'Space Grotesk',sans-serif;font-weight:700;letter-spacing:-0.02em}
`;
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const toks = (s) => esc(s).replace(/⟦[^⟧]+⟧/g, (t) => `<span class="tok">${t}</span>`);
const real = (s, values) => values.reduce((acc, v) => acc.replace(esc(v), `<span class="real">${esc(v)}</span>`), esc(s));
const page = (css, body) => `<!doctype html><html><head><meta charset="utf-8">${fonts}<style>${base}${css}</style></head><body>${body}</body></html>`;

// ---- synthetic example (engine output verified) -------------------------------------------
const VALUES = ["Dana Cohen", "000000018", "050-1234567", "jane@firm.org"];
const typed = "Summarize the hearing for our client Dana Cohen, ID 000000018, phone 050-1234567, and email the summary to jane@firm.org.";
const sent = "Summarize the hearing for our client ⟦לקוח_1⟧, ID ⟦תז_1⟧, phone ⟦טלפון_1⟧, and email the summary to ⟦מייל_1⟧.";
const reply = "Done. I emailed the summary to ⟦מייל_1⟧ and noted that ⟦לקוח_1⟧ can be reached at ⟦טלפון_1⟧.";
const restored = "Done. I emailed the summary to jane@firm.org and noted that Dana Cohen can be reached at 050-1234567.";
const FAKE_KEY = ["sk", "proj", "A1b2C3d4E5f6G7h8"].join("-");   // not a key; assembled so scanners stay quiet

const DETECTS = [
  ["Israeli ID (check digit)", 'ת"ז 000000018', 'ת"ז ⟦תז_1⟧'],
  ["Phone, any spelling", "+972 (0)52-000-0002", "⟦טלפון_1⟧"],
  ["E-mail", "jane@firm.org", "⟦מייל_1⟧"],
  ["Credit card (Luhn)", "4111 1111 1111 1111", "⟦אשראי_1⟧"],
  ["Case number", 'ת"פ 12345-06-23', 'ת"פ ⟦תיק_1⟧'],
  ["Street address", "מען: רחוב הדקל 12, נתניה", "מען: ⟦כתובת_1⟧"],
  ["Bank account", "חשבון 4567123", "חשבון ⟦חשבון_1⟧"],
  ["Vehicle plate", "123-45-678", "⟦רכב_1⟧"],
  ["Date of birth", "יליד 05.03.1980", "יליד ⟦לידה_1⟧"],
  ["Land parcel", "גוש 6789 חלקה 12", "⟦גושחלקה_1⟧"],
  ["API key / secret", FAKE_KEY, "⟦סוד_1⟧"],
  ["Client name, inflected", "לדנה כהן", "ל⟦לקוח_1⟧"],
];

// ---- pages ---------------------------------------------------------------------------------
const header = (dark) => page(`
  body{background:transparent}
  #s{display:inline-flex;align-items:center;gap:26px;padding:20px 28px}
  #s svg{width:112px;height:112px}
  .wm{font-size:96px;color:${dark ? "#F8FAFC" : C.ink}} .wm b{color:${dark ? C.teal2 : C.teal};font-weight:700}`,
  `<div id="s">${mark}<div class="wm">Hermes<b>Cloak</b></div></div>`);

const icon = (px) => page(`body{background:transparent} #s{width:${px}px;height:${px}px} #s svg{width:100%;height:100%;display:block}`,
  `<div id="s">${mark}</div>`);

const social = page(`
  #s{width:1280px;height:640px;background:${C.night};color:#F8FAFC;padding:84px 96px;position:relative;overflow:hidden}
  .row{display:flex;align-items:center;gap:28px} .row svg{width:120px;height:120px}
  .wm{font-size:92px} .wm b{color:${C.teal2}}
  h2{font-family:'Space Grotesk';font-weight:500;font-size:40px;line-height:1.25;margin-top:44px;color:#E2E8F0;max-width:1000px}
  .ex{margin-top:46px;font-size:27px;color:#CBD5E1;display:flex;gap:18px;align-items:center}
  .ex .real{background:#422006;color:#FCD34D} .ex .tok{background:#134E4A;color:#5EEAD4}
  .arrow{color:${C.teal2};font-size:30px}
  .feats{position:absolute;left:96px;bottom:70px;display:flex;gap:14px}
  .feats span{border:1.5px solid #1F2A3D;background:#111A2C;border-radius:999px;padding:10px 20px;font-size:21px;color:#CBD5E1}
  .glow{position:absolute;right:-160px;top:-160px;width:560px;height:560px;border-radius:50%;
        background:radial-gradient(circle,rgba(20,184,166,.28),rgba(20,184,166,0) 70%)}`,
  `<div id="s"><div class="glow"></div>
    <div class="row">${mark}<div class="wm">Hermes<b>Cloak</b></div></div>
    <h2>Your agent uses a cloud LLM.<br>Your clients' personal data never reaches it.</h2>
    <div class="ex"><span class="real">Dana Cohen · 050-1234567</span><span class="arrow">→</span>
      <span class="tok">⟦לקוח_1⟧</span><span class="tok">⟦טלפון_1⟧</span><span class="arrow">→</span>
      <span>restored locally</span></div>
    <div class="feats"><span>Hebrew-first detection</span><span>Fail-closed</span><span>Plugin for hermes-agent</span><span>Apache-2.0</span></div>
  </div>`);

const beforeAfter = page(`
  #s{width:1280px;background:${C.bg};padding:44px 48px 36px}
  .cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:22px;align-items:stretch}
  .col{border-radius:18px;padding:22px 22px 24px;display:flex;flex-direction:column;gap:14px}
  .local{background:${C.card};border:1.5px solid ${C.line}}
  .cloud{background:#ECFEFF;border:1.5px dashed ${C.teal2}}
  .tag{font-size:14px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:${C.slate};display:flex;gap:8px;align-items:center}
  .tag i{width:10px;height:10px;border-radius:50%;display:inline-block}
  h3{font-family:'Space Grotesk';font-size:25px;font-weight:600}
  .msg{background:${C.bg};border-radius:12px;padding:14px 16px;font-size:18.5px;line-height:1.75}
  .cloud .msg{background:#FFFFFF}
  .who{font-size:13.5px;color:${C.mute};font-weight:600;margin-bottom:2px}
  .foot{display:flex;gap:14px;margin-top:24px;flex-wrap:wrap}
  .pill{background:${C.card};border:1.5px solid ${C.line};border-radius:999px;padding:9px 16px;font-size:16px;color:${C.slate}}
  .pill b{color:${C.teal}}`,
  `<div id="s"><div class="cols">
    <div class="col local"><div class="tag"><i style="background:${C.amber}"></i>On your machine</div>
      <h3>1 · What you type</h3>
      <div><div class="who">you → agent</div><div class="msg">${real(typed, VALUES)}</div></div></div>
    <div class="col cloud"><div class="tag"><i style="background:${C.teal2}"></i>Cloud LLM</div>
      <h3>2 · What the model sees</h3>
      <div><div class="who">request</div><div class="msg">${toks(sent)}</div></div>
      <div><div class="who">reply</div><div class="msg">${toks(reply)}</div></div></div>
    <div class="col local"><div class="tag"><i style="background:${C.amber}"></i>On your machine</div>
      <h3>3 · What you get back</h3>
      <div><div class="who">agent → you</div><div class="msg">${real(restored, VALUES)}</div></div></div>
  </div>
  <div class="foot">
    <span class="pill mono"><b>real_values_in_outbound: 0</b></span>
    <span class="pill">Same value → same token, all conversation long</span>
    <span class="pill">Tools still run with the real values</span>
  </div></div>`);

const box = (cls, title, lines) => `<div class="box ${cls}"><h4>${title}</h4>${lines.map((l) => `<p>${l}</p>`).join("")}</div>`;
const howItWorks = page(`
  #s{width:1280px;background:${C.bg};padding:40px 44px 34px}
  .zone{border-radius:20px;padding:20px 22px 22px;position:relative}
  .zlabel{font-size:14px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin-bottom:14px}
  .mine{background:#FFFFFF;border:1.5px solid ${C.line}} .mine .zlabel{color:${C.amber}}
  .wrap{display:grid;grid-template-columns:1fr 250px;gap:22px;align-items:stretch}
  .cloudz{background:#ECFEFF;border:1.5px dashed ${C.teal2};display:flex;flex-direction:column;justify-content:center}
  .cloudz .zlabel{color:${C.teal}}
  .flow{display:grid;grid-template-columns:1fr 34px 1.25fr;gap:10px;align-items:stretch}
  .box{border-radius:14px;padding:14px 16px;background:${C.bg};border:1.5px solid ${C.line}}
  .box h4{font-family:'Space Grotesk';font-size:20px;font-weight:600;margin-bottom:6px}
  .box p{font-size:15.5px;line-height:1.55;color:${C.slate}}
  .cloak{background:#F0FDFA;border-color:${C.teal2}} .cloak h4{color:${C.teal}}
  .vault{background:${C.amberbg};border-color:#FCD34D} .vault h4{color:${C.amber}}
  .arr{display:flex;align-items:center;justify-content:center;color:${C.teal};font-size:26px;font-weight:700}
  .stack{display:flex;flex-direction:column;gap:12px}
  .seams{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}
  .seam{background:${C.card};border:1.5px solid ${C.line};border-radius:12px;padding:10px 12px;font-size:14.5px;line-height:1.45;color:${C.slate}}
  .seam b{display:block;color:${C.ink};font-size:15.5px;margin-bottom:2px}
  .llm{background:#FFFFFF;border:1.5px solid ${C.line};border-radius:14px;padding:16px;text-align:center}
  .llm h4{font-family:'Space Grotesk';font-size:21px} .llm p{font-size:15px;color:${C.slate};margin-top:6px;line-height:1.5}
  .side{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:6px;color:${C.teal};font-weight:700;font-size:15px}
  .fc{margin-top:16px;display:flex;gap:12px;flex-wrap:wrap}
  .fc span{border-radius:999px;padding:8px 14px;font-size:15px;background:${C.ink};color:#F8FAFC}
  .fc span.l{background:#FFFFFF;color:${C.slate};border:1.5px solid ${C.line}}`,
  `<div id="s"><div class="wrap">
    <div class="zone mine"><div class="zlabel">Your machine · hermes-agent + HermesCloak plugin</div>
      <div class="flow">
        <div class="stack">
          ${box("", "You / your channels", ["CLI, Telegram, WhatsApp, e-mail…", "Real names, IDs, phones, case files"])}
          ${box("", "Tools run locally", ["File, mail and CRM tools get the <b>real</b> values back in their arguments"])}
        </div>
        <div class="arr">⇄</div>
        <div class="stack">
          ${box("cloak", "Tokenize → send", ["Every outbound request: personal data becomes <span class='tok'>⟦לקוח_1⟧</span>, <span class='tok'>⟦תז_1⟧</span>…", "A missing seam or a load error withholds the send (fail-closed)"])}
          ${box("vault", "Local vault", ["token ↔ value, in $HERMES_HOME/cloak", "crash-safe journal · 0600 · TTL · optional encryption"])}
          ${box("cloak", "Restore ← receive", ["Replies, streamed deltas and tool-call arguments get the real values back before you or a tool sees them"])}
        </div>
      </div>
      <div class="seams">
        <div class="seam"><b>Outbound</b>llm_request middleware</div>
        <div class="seam"><b>Inbound</b>every transport's response</div>
        <div class="seam"><b>Streaming</b>deltas restored on the fly</div>
        <div class="seam"><b>Auxiliary</b>titles, summaries, compression</div>
      </div>
    </div>
    <div class="zone cloudz"><div class="zlabel">Cloud</div>
      <div class="llm"><h4>Any LLM provider</h4><p>OpenAI, Anthropic, Gemini, OpenRouter…</p><p>Sees tokens only — never the values</p></div>
    </div>
  </div>
  <div class="fc"><span>hermes hermescloak verify</span><span>hermes hermescloak test</span>
    <span class="l">Directory plugin — no fork, no patch to hermes-agent</span><span class="l">Audit log of every send</span></div>
  </div>`);

const detects = page(`
  #s{width:1280px;background:${C.bg};padding:36px 40px}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
  .c{background:${C.card};border:1.5px solid ${C.line};border-radius:14px;padding:14px 16px 16px}
  .c h5{font-size:14px;font-weight:700;color:${C.slate};letter-spacing:.02em;margin-bottom:10px}
  .v{font-size:17px;line-height:1.5;font-family:Heebo,Inter,sans-serif}
  .v.in{color:${C.amber}} .d{color:${C.mute};font-size:13px;margin:4px 0}
  .note{margin-top:18px;font-size:15.5px;color:${C.slate}}`,
  `<div id="s"><div class="grid">${DETECTS.map(([t, i, o]) => `
    <div class="c"><h5>${t}</h5><div class="v in" dir="auto"><span class="real">${esc(i)}</span></div>
      <div class="d">↓ sent to the cloud as</div><div class="v" dir="auto">${toks(o)}</div></div>`).join("")}
  </div><div class="note">Synthetic examples; the bottom line of each card is the engine's real output. Courts, judges, lawyers, statutes and amounts are left as they are.</div></div>`);

const termHtml = esc(terminal)
  .replace(/^(\$ .*)$/gm, `<span class="cmd">$1</span>`)
  .replace(/\[OK \]/g, `<span class="ok">[OK ]</span>`)
  .replace(/\[info\]/g, `<span class="info">[info]</span>`)
  .replace(/(PROTECTED ✓|protected ✓)/g, `<span class="ok">$1</span>`)
  .replace(/^(---.*---)$/gm, (m) => m.includes("class=") ? m : `<span class="sec">${m}</span>`);
const verifyTest = page(`
  #s{width:1280px;background:${C.bg};padding:32px}
  .win{background:${C.night};border-radius:14px;overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,.18)}
  .bar{height:38px;background:#111827;display:flex;align-items:center;gap:8px;padding:0 16px}
  .bar i{width:12px;height:12px;border-radius:50%;display:block}
  pre{font-family:'JetBrains Mono',monospace;font-size:14.5px;line-height:1.6;color:#CBD5E1;padding:18px 22px 22px;white-space:pre}
  .cmd{color:#F8FAFC;font-weight:600} .ok{color:#34D399;font-weight:600} .info{color:#60A5FA} .sec{color:#94A3B8}`,
  `<div id="s"><div class="win"><div class="bar"><i style="background:#F87171"></i><i style="background:#FBBF24"></i><i style="background:#34D399"></i></div>
   <pre>${termHtml}</pre></div></div>`);

// ---- render --------------------------------------------------------------------------------
const jobs = [
  ["header-light.png", header(false), true, brand],
  ["header-dark.png", header(true), true, brand],
  ["icon-32.png", icon(32), true, brand, 1],
  ["icon-180.png", icon(180), true, brand, 1],
  ["icon-512.png", icon(512), true, brand, 1],
  ["social-preview.png", social, false, images, 1],
  ["before-after.png", beforeAfter, false, images],
  ["how-it-works.png", howItWorks, false, images],
  ["detects.png", detects, false, images],
  ["verify-test.png", verifyTest, false, images],
];
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
async function shoot(name, html, transparent, dir, scale) {
  const p = await browser.newPage({ deviceScaleFactor: scale, viewport: { width: 1400, height: 900 } });
  try {
    await p.setContent(html, { waitUntil: "load" });
    const ok = await p.evaluate(async () => {
      const want = ["700 20px 'Space Grotesk'", "500 20px 'Space Grotesk'", "400 20px Inter", "700 20px Inter",
                    "400 20px 'JetBrains Mono'", "400 20px Heebo", "700 20px Heebo", "400 20px 'Noto Sans Math'"];
      await Promise.all(want.map((f) => document.fonts.load(f, "Aaא⟦")));
      await document.fonts.ready;
      return want.every((f) => document.fonts.check(f, "Aa"));
    });
    if (!ok) throw new Error("web fonts did not load");
    await p.locator("#s").screenshot({ path: join(dir, name), omitBackground: transparent });
  } finally { await p.close(); }
}
for (const [name, html, transparent, dir, scale = 2] of jobs) {
  for (let attempt = 1; ; attempt++) {
    try { await shoot(name, html, transparent, dir, scale); break; }
    catch (e) { if (attempt >= 4) throw e; console.log(`retry ${name}: ${e.message.split("\n")[0]}`); }
  }
  console.log("wrote", join(dir, name));
}
await browser.close();
