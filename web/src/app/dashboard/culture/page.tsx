import { redirect } from "next/navigation";

// 컬처는 팀/컬처 페이지(/dashboard/team) 상단으로 통합됐다.
// 기존 링크/북마크 호환을 위해 redirect만 남긴다.
export default function CulturePage() {
  redirect("/dashboard/team");
}
