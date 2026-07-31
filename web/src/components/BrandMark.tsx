import Image from "next/image";
import { cx } from "./ui";

type BrandMarkProps = {
  className?: string;
  size?: "sm" | "md" | "lg";
};

const SIZE_CLASS: Record<NonNullable<BrandMarkProps["size"]>, string> = {
  sm: "h-6 w-6",
  md: "h-8 w-8",
  lg: "h-9 w-9",
};

const SIZE_PX: Record<NonNullable<BrandMarkProps["size"]>, number> = {
  sm: 24,
  md: 32,
  lg: 36,
};

export function BrandMark({ className, size = "md" }: BrandMarkProps) {
  return (
    <span
      className={cx(
        "inline-flex shrink-0 items-center justify-center overflow-hidden",
        SIZE_CLASS[size],
        className,
      )}
      aria-hidden
    >
      <Image
        alt=""
        className="h-full w-full"
        draggable={false}
        height={SIZE_PX[size]}
        src="/orthus-mark.svg"
        width={SIZE_PX[size]}
      />
    </span>
  );
}
