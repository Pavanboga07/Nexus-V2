/** classnames joiner: strings, false and undefined only. */
export function cn(...parts: Array<string | false | undefined>): string {
  return parts.filter((part) => part !== false && part !== undefined).join(" ");
}
