import { Suspense } from "react";
import MoreClient from "@/app/components/MoreClient";

export const dynamic = "force-dynamic";

export default function MorePage() {
  return (
    <Suspense>
      <MoreClient />
    </Suspense>
  );
}
