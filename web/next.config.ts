import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  devIndicators: false,
  allowedDevOrigins: [
    "orthus-central.example.com",
    "orthus-team.example.com",
    "orthus-personal.example.com",
  ],
  experimental: {
    // 클라이언트 라우터 캐시 — 방문한 라우트 세그먼트를 재사용해 back-nav/재방문 시
    // RSC 왕복 없이 즉시 전환한다. 데이터 신선도는 각 페이지의 client fetch
    // (+use-cached-resource SWR)가 담당하므로 페이지 내용 stale 위험은 없다.
    staleTimes: {
      dynamic: 60,
      static: 300,
    },
  },
};

export default nextConfig;
