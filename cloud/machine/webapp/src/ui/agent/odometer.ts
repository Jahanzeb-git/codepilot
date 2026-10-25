/**
 * Slot-machine digit counter.
 *
 * Each digit is a vertical strip of 0–9 translated inside a one-line window,
 * so incrementing 3 → 4 rolls the strip instead of swapping a character. When
 * the number gains a digit (9 → 10) the new column is built and rolls in, which
 * keeps the live tool-call counter feeling physical rather than numeric.
 */

const DIGIT_HEIGHT = 14;

export class Odometer {
  private host: HTMLElement;
  private strips: HTMLElement[] = [];
  private value = -1;

  constructor(host: HTMLElement) {
    this.host = host;
    this.host.classList.add("odometer");
    this.host.setAttribute("aria-hidden", "true");
    this.set(0, true);
  }

  set(next: number, immediate = false): void {
    const clamped = Math.max(0, Math.floor(next));
    if (clamped === this.value) return;

    const digits = String(clamped);
    const widthChanged = digits.length !== this.strips.length;
    if (widthChanged) this.build(digits.length, immediate);

    this.strips.forEach((strip, index) => {
      const digit = Number(digits[index] ?? 0);
      const offset = `translateY(-${digit * DIGIT_HEIGHT}px)`;
      if (immediate || widthChanged) {
        strip.style.transition = "none";
        strip.style.transform = offset;
        // Re-enable the roll on the next frame so the initial paint is silent
        // but later changes animate.
        requestAnimationFrame(() => {
          strip.style.transition = "";
        });
      } else {
        strip.style.transform = offset;
      }
    });

    this.value = clamped;
  }

  private build(width: number, immediate: boolean): void {
    this.host.replaceChildren();
    this.strips = [];

    const pad = width > 1 ? "0" : "";
    for (let column = 0; column < width; column += 1) {
      const digitEl = document.createElement("span");
      digitEl.className = "od-digit";

      const strip = document.createElement("span");
      strip.className = "od-strip";
      if (immediate) strip.style.transition = "none";

      // One extra "0" at the tail lets 9 roll forward to 0 during the
      // wrap without a visible jump back to the top.
      for (let n = 0; n <= 10; n += 1) {
        const cell = document.createElement("span");
        cell.textContent = String(n % 10);
        strip.appendChild(cell);
      }

      digitEl.appendChild(strip);
      this.host.appendChild(digitEl);
      this.strips.push(strip);

      if (immediate && pad) {
        strip.style.transform = "translateY(0px)";
      }
    }
  }
}
