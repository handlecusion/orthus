import { redirect } from "next/navigation";

// 첫 진입 화면 = 위키 홈. 웹·폰 모두 동일하게 지식 워크스페이스로 보낸다(UA 분기 없음).
export default function Home() {
  redirect("/wiki");
}
