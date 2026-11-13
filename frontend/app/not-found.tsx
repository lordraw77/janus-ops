import Link from "next/link";

export default function NotFound() {
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <Link href="/" className="text-sm text-primary hover:text-primary-hover">
        Back to the dashboard
      </Link>
    </div>
  );
}
