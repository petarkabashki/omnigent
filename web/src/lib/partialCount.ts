/**
 * Render a count taken from a possibly partial child list: ``"20+"`` when
 * older children exist that the list does not hold, the plain number otherwise.
 */
export function partialCountLabel(count: number, partial: boolean): string {
  return partial ? `${count}+` : String(count);
}

/**
 * Agents badge text: ``"2/5"`` while some work, else ``"5"``; every figure
 * carries ``+`` when the child list is partial.
 */
export function agentsBadgeLabel(working: number, agentCount: number, partial: boolean): string {
  const total = partialCountLabel(agentCount, partial);
  return working > 0 ? `${partialCountLabel(working, partial)}/${total}` : total;
}
