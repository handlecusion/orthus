import { redirect } from "next/navigation";

// 메모는 좌측 "개인" 섹션의 /notes로 옮겼다(docs/personal-notes.md). 기존
// /dashboard/memo 진입점은 그대로 리다이렉트해 북마크/링크를 보존한다.
export default function MemoRedirect() {
  redirect("/notes");
}
