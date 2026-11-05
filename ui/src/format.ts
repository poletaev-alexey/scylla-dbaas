/** "rolling_restart" -> "Rolling restart" */
export function label(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1).replaceAll("_", " ");
}

/**
 * Parse an API timestamp. The Control Plane currently serialises UTC datetimes without an
 * offset ("2026-11-05T13:54:08.99"), which browsers would read as local time; treat
 * offset-less values as UTC.
 */
export function parseTs(s: string): Date {
  return new Date(/(Z|[+-]\d\d:?\d\d)$/.test(s) ? s : `${s}Z`);
}
