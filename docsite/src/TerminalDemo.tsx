import { useEffect, useRef, useState } from "react";

// Max lines visible in the terminal body at once.
// Keeps the component from growing beyond its fixed height (300px ÷ ~19px/line ≈ 15).
const MAX_LINES = 15;

// Each frame is either a line to print (type: "line") or a pause (type: "pause").
// delay = ms to wait before showing this frame.
type Frame =
  | { kind: "type"; text: string; speed: number }   // typing animation for the command
  | { kind: "line"; text: string; cls?: string; delay: number }  // instant line
  | { kind: "bar"; pkg: string; size: string; delay: number }    // animated progress bar
  | { kind: "pause"; delay: number };                // silent pause

const FRAMES: Frame[] = [
  // Pause before starting (feels like a fresh terminal)
  { kind: "pause", delay: 600 },

  // Type the command
  { kind: "type", text: "pip install codepilot-ai", speed: 68 },

  // Pause before output (Enter was pressed)
  { kind: "pause", delay: 340 },

  // --- Collecting phase ---
  { kind: "line", text: "Collecting codepilot-ai", cls: "t-dim", delay: 80 },
  { kind: "line", text: "  Downloading codepilot_ai-0.9.21-py3-none-any.whl.metadata (12 kB)", cls: "t-dim", delay: 60 },
  { kind: "line", text: "Collecting pydantic>=2.0", cls: "t-dim", delay: 55 },
  { kind: "line", text: "  Downloading pydantic-2.13.4-py3-none-any.whl.metadata (109 kB)", cls: "t-dim", delay: 55 },
  { kind: "line", text: "Collecting openai>=1.0", cls: "t-dim", delay: 55 },
  { kind: "line", text: "  Downloading openai-2.38.0-py3-none-any.whl.metadata (31 kB)", cls: "t-dim", delay: 55 },
  { kind: "line", text: "Collecting anthropic>=0.3", cls: "t-dim", delay: 55 },
  { kind: "line", text: "  Downloading anthropic-0.104.1-py3-none-any.whl.metadata (3.2 kB)", cls: "t-dim", delay: 55 },
  { kind: "line", text: "Collecting python-dotenv>=1.0", cls: "t-dim", delay: 55 },
  { kind: "line", text: "Collecting rich>=13.0", cls: "t-dim", delay: 55 },
  { kind: "line", text: "Collecting jinja2>=3.1", cls: "t-dim", delay: 55 },
  { kind: "line", text: "Collecting pexpect>=4.8", cls: "t-dim", delay: 55 },
  { kind: "line", text: "Collecting tiktoken>=0.5", cls: "t-dim", delay: 55 },

  // --- Download phase with progress bars ---
  { kind: "pause", delay: 120 },
  { kind: "line", text: "Downloading codepilot_ai-0.9.21-py3-none-any.whl (103 kB)", cls: "t-dim", delay: 0 },
  { kind: "bar", pkg: "anthropic-0.104.1", size: "832 kB", delay: 80 },
  { kind: "bar", pkg: "pydantic-2.13.4", size: "472 kB", delay: 80 },
  { kind: "bar", pkg: "pydantic_core-2.46.4", size: "2.1 MB", delay: 80 },
  { kind: "bar", pkg: "openai-2.38.0", size: "1.3 MB", delay: 80 },
  { kind: "bar", pkg: "tiktoken-0.13.0", size: "1.1 MB", delay: 80 },
  { kind: "bar", pkg: "pygments-2.20.0", size: "1.2 MB", delay: 80 },
  { kind: "bar", pkg: "rich-15.0.0", size: "310 kB", delay: 80 },
  { kind: "bar", pkg: "jinja2-3.1.6", size: "134 kB", delay: 70 },
  { kind: "bar", pkg: "httpx-0.28.1", size: "73 kB", delay: 70 },
  { kind: "bar", pkg: "anyio-4.13.0", size: "114 kB", delay: 70 },

  // --- Installing ---
  { kind: "pause", delay: 140 },
  {
    kind: "line",
    text: "Installing collected packages: pydantic-core, pydantic, anthropic,",
    cls: "t-dim",
    delay: 0,
  },
  {
    kind: "line",
    text: "  openai, tiktoken, rich, jinja2, httpx, anyio, codepilot-ai, ...",
    cls: "t-dim",
    delay: 0,
  },
  { kind: "pause", delay: 340 },

  // Success
  {
    kind: "line",
    text: "Successfully installed codepilot-ai-0.9.21",
    cls: "t-success",
    delay: 0,
  },

  // Final prompt
  { kind: "pause", delay: 200 },
  { kind: "line", text: "", delay: 0 },

  // Hold the completed state for 3.5 seconds then loop
  { kind: "pause", delay: 3500 },
];

// ---

interface RenderedLine {
  text: string;
  cls?: string;
  id: number;
}

function ProgressBar({ pkg, size }: { pkg: string; size: string }) {
  const [width, setWidth] = useState(0);
  const raf = useRef<number | null>(null);
  const start = useRef<number | null>(null);
  const DURATION = 520; // ms for bar to fill

  useEffect(() => {
    const animate = (ts: number) => {
      if (start.current === null) start.current = ts;
      const p = Math.min((ts - start.current) / DURATION, 1);
      setWidth(p * 100);
      if (p < 1) raf.current = requestAnimationFrame(animate);
    };
    raf.current = requestAnimationFrame(animate);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); };
  }, []);

  const filled = Math.round(width / 100 * 38);
  const empty = 38 - filled;
  const bar = "━".repeat(filled) + (width < 100 ? "╸" : "") + "━".repeat(Math.max(0, empty - (width < 100 ? 1 : 0)));

  return (
    <span className="t-bar-line">
      <span className="t-bar-track">{bar}</span>
      {" "}<span className="t-dim">{size}</span>
      {" "}<span className="t-dim2">[{pkg}]</span>
    </span>
  );
}

// ---

export function TerminalDemo() {
  const [lines, setLines] = useState<RenderedLine[]>([]);
  const [typedText, setTypedText] = useState("");
  const [showCursor, setShowCursor] = useState(true);
  const [barFrames, setBarFrames] = useState<Map<number, { pkg: string; size: string }>>(new Map());

  const counter = useRef(0);
  const running = useRef(true);

  // Blink cursor
  useEffect(() => {
    const t = setInterval(() => setShowCursor((c) => !c), 530);
    return () => clearInterval(t);
  }, []);

  // Main animation loop
  useEffect(() => {
    running.current = true;
    let cancelled = false;

    const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

    async function run() {
      while (!cancelled) {
        // Reset
        setLines([]);
        setTypedText("");
        setBarFrames(new Map());

        for (const frame of FRAMES) {
          if (cancelled) return;

          if (frame.kind === "pause") {
            await sleep(frame.delay);
          } else if (frame.kind === "type") {
            // Type character by character
            for (let i = 0; i <= frame.text.length; i++) {
              if (cancelled) return;
              setTypedText(frame.text.slice(0, i));
              await sleep(frame.speed + (Math.random() - 0.5) * 30);
            }
            // Brief pause after typing before "enter"
            await sleep(280);
            // Commit the typed line to lines and clear typedText
            const id = ++counter.current;
            setLines((prev) => [
              ...prev.slice(-MAX_LINES + 1),
              { text: "$ " + frame.text, cls: "t-cmd", id },
            ]);
            setTypedText("");
          } else if (frame.kind === "line") {
            await sleep(frame.delay);
            if (frame.text !== "") {
              const id = ++counter.current;
              setLines((prev) => [
                ...prev.slice(-MAX_LINES + 1),
                { text: frame.text, cls: frame.cls, id },
              ]);
            } else {
              const id = ++counter.current;
              setLines((prev) => [...prev.slice(-MAX_LINES + 1), { text: "", id }]);
            }
          } else if (frame.kind === "bar") {
            await sleep(frame.delay);
            const id = ++counter.current;
            setBarFrames((prev) => new Map(prev).set(id, { pkg: frame.pkg, size: frame.size }));
            setLines((prev) => [...prev.slice(-MAX_LINES + 1), { text: "", cls: "t-bar", id }]);
            // Wait for bar animation to finish
            await sleep(560);
          }
        }
      }
    }

    run();
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="term-window" aria-hidden="true">
      {/* Title bar */}
      <div className="term-titlebar">
        <span className="term-dot term-dot-red" />
        <span className="term-dot term-dot-yellow" />
        <span className="term-dot term-dot-green" />
        <span className="term-title">bash</span>
      </div>

      {/* Terminal body */}
      <div className="term-body">

        {lines.map((line) => {
          const barData = barFrames.get(line.id);
          if (barData) {
            return (
              <div key={line.id} className="term-line">
                {"  "}<ProgressBar pkg={barData.pkg} size={barData.size} />
              </div>
            );
          }
          if (line.text === "") return <div key={line.id} className="term-line"> </div>;
          return (
            <div key={line.id} className={`term-line ${line.cls ?? ""}`}>
              {line.text}
            </div>
          );
        })}

        {/* Live typing row */}
        {typedText !== "" || lines.length === 0 ? (
          <div className="term-line term-input-row">
            <span className="t-user">user</span>
            <span className="t-at">@</span>
            <span className="t-host">local</span>
            <span className="t-colon">:</span>
            <span className="t-path">~</span>
            <span className="t-dollar"> $ </span>
            <span className="t-typed">{typedText}</span>
            <span className={`term-cursor ${showCursor ? "term-cursor-on" : ""}`} />
          </div>
        ) : null}
      </div>
    </div>
  );
}
