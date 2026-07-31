import { redirect } from "next/navigation";

// 통합 "설정" 진입점. 첫 탭(접근 관리)으로 보낸다. 탭바는 settings/layout.tsx.
export default function SettingsIndexPage() {
  redirect("/settings/access");
}
