import type { ReactNode } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

/**
 * Agent dropdown shared by the fork and switch-engine dialogs: built-ins, a
 * divider, then custom agents. `leadingItem` adds a first option (the fork
 * dialog's "same as original"); `selectedContent` overrides the trigger text.
 */
export function AgentTargetSelect({
  id,
  testIdPrefix,
  componentId,
  value,
  onValueChange,
  builtins,
  customs,
  leadingItem,
  selectedContent,
  placeholder,
  disabled,
}: {
  id: string;
  testIdPrefix: string;
  componentId: string;
  value: string;
  onValueChange: (value: string) => void;
  builtins: readonly AvailableAgent[];
  customs: readonly AvailableAgent[];
  leadingItem?: ReactNode;
  selectedContent?: ReactNode;
  /** Shown while `value` is empty; Radix ignores `selectedContent` then. */
  placeholder?: ReactNode;
  disabled?: boolean;
}) {
  const option = (agent: AvailableAgent) => (
    <SelectItem
      key={agent.id}
      value={agent.id}
      data-testid={`${testIdPrefix}-option-${agent.id}`}
      className="text-sm"
    >
      {agent.display_name}
    </SelectItem>
  );
  return (
    <Select
      value={value}
      onValueChange={onValueChange}
      componentId={componentId}
      disabled={disabled}
    >
      <SelectTrigger id={id} data-testid={`${testIdPrefix}-select`} className="w-full text-sm">
        <SelectValue placeholder={placeholder}>{selectedContent}</SelectValue>
      </SelectTrigger>
      <SelectContent position="popper" align="start">
        {leadingItem}
        {builtins.map(option)}
        {/* Divider between built-ins and customs, only when both are present. */}
        {builtins.length > 0 && customs.length > 0 && <SelectSeparator />}
        {customs.map(option)}
      </SelectContent>
    </Select>
  );
}
