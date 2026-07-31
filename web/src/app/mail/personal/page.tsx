import { redirect } from "next/navigation";

// 개인 메일은 통합 `/mail` 화면의 "개인 Gmail" 메일함으로 합쳐졌다.
// 기존 북마크/링크 호환을 위해 영구 리다이렉트만 남긴다.
export default function PersonalMailRedirect() {
  redirect("/mail");
}
