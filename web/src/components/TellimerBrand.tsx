import tellimerMark from "@/assets/tellimer-mark.svg";
import { cn } from "@/lib/utils";

interface TellimerBrandProps {
  compact?: boolean;
  className?: string;
}

/** Tellimer's official mark and the internal product lockup. */
export function TellimerBrand({ compact = false, className }: TellimerBrandProps) {
  return (
    <span
      className={cn("inline-flex min-w-0 items-center", compact ? "gap-2.5" : "gap-3.5", className)}
      data-testid="tellimer-brand"
    >
      <span
        className={cn(
          "grid shrink-0 place-items-center border border-border bg-[#f0edeb]",
          compact ? "size-8 rounded-lg" : "size-12 rounded-xl",
        )}
      >
        <img src={tellimerMark} alt="" className={cn(compact ? "h-5 w-auto" : "h-7 w-auto")} />
      </span>
      <span className="flex min-w-0 flex-col text-left">
        <span
          className={cn(
            "truncate font-semibold tracking-[-0.025em] text-foreground",
            compact ? "text-[15px] leading-4" : "text-xl leading-6",
          )}
        >
          Tellimer
        </span>
        <span
          className={cn(
            "truncate font-medium uppercase tracking-[0.12em] text-muted-foreground",
            compact ? "text-[8px] leading-3" : "text-[10px] leading-4",
          )}
        >
          Agent Platform
        </span>
      </span>
    </span>
  );
}
