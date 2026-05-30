# FE API Notes

**Production API base URL:** `https://api.pagong.dev`  
(Vercel: `NEXT_PUBLIC_API_URL=https://api.pagong.dev` — `http://52.78.185.67:8000` 사용 금지)

## 공유 링크 목록

`GET /api/share-links` (MANAGER / EXECUTIVE, Bearer 토큰)

선택 쿼리: `projectId`, `fileId`, `status`

## 공유 링크 생성

`POST /api/files/{file_id}/share-links`

FE가 보내는 값:

```json
{
  "clientId": 1,
  "expiresInDays": 7,
  "assignedStaffUserId": 1
}
```

- `clientId`: 고객사 ID입니다. `GET /api/clients`로 조회한 값을 사용합니다.
- `expiresInDays`: 공유 링크 만료 기간입니다. `1`, `3`, `7`만 허용됩니다.
- `assignedStaffUserId`: 담당 직원 ID입니다. 선택값이며, 해당 프로젝트의 `EMPLOYEE` 멤버여야 합니다.

FE가 보내지 않는 값:

- `projectId`: 서버가 `file_id`로 파일을 조회해서 자동 계산합니다.
- `createdBy`: 서버가 로그인 토큰의 사용자 ID로 자동 저장합니다.
- `note`: 사용하지 않으므로 제거했습니다.

응답 예시:

```json
{
  "shareLinkId": 1,
  "fileId": 3,
  "projectId": 2,
  "token": "generated-token",
  "url": "https://api.pagong.dev/api/share-links/generated-token/download",
  "infoUrl": "https://api.pagong.dev/api/share-links/generated-token",
  "clientId": 1,
  "clientName": "A뷰티",
  "createdBy": 2,
  "assignedStaffUserId": 1,
  "expiresAt": "2026-06-04T10:00:00",
  "status": "ACTIVE"
}
```

## 고객사 목록

`GET /api/clients`

응답 예시:

```json
{
  "clients": [
    {
      "clientId": 1,
      "clientName": "A뷰티"
    },
    {
      "clientId": 2,
      "clientName": "B식품"
    }
  ]
}
```

## 프로젝트 생성

`POST /api/projects`

```json
{
  "name": "A뷰티 여름 캠페인",
  "clientId": 1,
  "description": "여름 신제품 런칭 캠페인",
  "members": [
    {
      "userId": 1,
      "projectRole": "MEMBER"
    }
  ]
}
```
