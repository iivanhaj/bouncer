"use client";

import { usePathname, useRouter } from "next/navigation";

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { RANGES } from "@/lib/range";


/** Time range as a URL parameter, so the server fetches exactly that window. */
export function RangeToggle({ days }: { days: number }) {
  const router = useRouter();
  const pathname = usePathname();

  return (
    <ToggleGroup
      type="single"
      size="sm"
      variant="outline"
      value={String(days)}
      onValueChange={(value) => value && router.push(`${pathname}?days=${value}`)}
    >
      {RANGES.map((range) => (
        <ToggleGroupItem key={range.days} value={String(range.days)} className="px-3">
          {range.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
