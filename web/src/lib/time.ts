/** In-game seconds as gem shows them: "42:49", "-01:00" before the horn. */
export function clock(seconds: number): string {
  const whole = seconds < 0 ? Math.ceil(-seconds - 1e-6) : Math.floor(seconds + 1e-6);
  const text = `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
  return seconds < 0 ? `-${text}` : text;
}

/** A duration as "m:ss". */
export function span(seconds: number): string {
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}
