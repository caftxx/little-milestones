# List all albums

> 获取所有相册列表

检索当前认证用户可访问的相册列表。

---

**状态**: Stable
**权限**: `album.read`

---

## 端点信息

| 属性 | 值 |
|------|-----|
| 方法 | `GET` |
| 路径 | `/albums` |
| 名称 | `getAllAlbums` |

---

## 查询参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| `assetId` | UUID | ❌ | 筛选包含指定资源 ID 的相册（忽略 shared 参数） |
| `shared` | Boolean | ❌ | 按共享状态筛选：`true` = 仅共享相册，`false` = 未共享相册，不指定 = 所有拥有相册 |

---

## 响应

| 状态码 | 类型 | 描述 |
|--------|------|------|
| `200` | [AlbumResponseDto[]](https://api.immich.app/models/AlbumResponseDto) | 相册响应数组 |

---

## 历史版本

| 版本 | 状态 |
|------|------|
| v2 | Stable |
| v1 | Beta |
| v1 | Added |

---

## 使用示例

### 请求示例

```http
GET /albums
GET /albums?shared=true
GET /albums?assetId=uuid-here
