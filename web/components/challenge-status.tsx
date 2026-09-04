interface AvailabilityBadgeProps {
  readonly open: boolean;
  readonly prominent?: boolean;
}

export function AvailabilityBadge({
  open,
  prominent = false,
}: AvailabilityBadgeProps) {
  const className = [
    "availability",
    open ? "availability--open" : "availability--closed",
    prominent ? "availability--prominent" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={className}>
      <span className="availability__mark" aria-hidden="true" />
      {open ? "提交已开放" : "提交未开放"}
    </span>
  );
}
