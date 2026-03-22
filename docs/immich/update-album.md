# Update an album

> 更新相册信息

更新指定 ID 的相册信息。此端点可用于更新相册名称、描述、排序顺序等。但是，它不用于添加或删除相册中的资产或用户。

---

**状态**: Stable
**权限**: `album.update`

---

## 端点信息

| 属性 | 值 |
|------|-----|
| 方法 | `PATCH` |
| 路径 | `/albums/{id}` |
| 名称 | `updateAlbumInfo` |

---

## 参数 (Parameters)

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| `id` | UUID | ✅ | 相册 ID |

---

## 请求体 (Request)

| 字段名 | 类型 | 描述 |
|--------|------|------|
| `albumName` | String | 相册名称 |
| `albumThumbnailAssetId` | UUID | 相册缩略图资源 ID |
| `description` | String | 相册描述 |
| `isActivityEnabled` | Boolean | 启用活动动态 |
| `order` | AssetOrder | 资源排序顺序 |

---

## 响应体 (Response)

| 字段名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| `albumName` | String | ✅ | 相册名称 |
| `albumThumbnailAssetId` | String / Null | ✅ | 缩略图资源 ID |
| `albumUsers` | AlbumUserResponseDto[] | ✅ | 相册用户列表 |
| `assetCount` | Number | ✅ | 资源数量 |
| `assets` | AssetResponseDto[] | ✅ | 资源列表 |
| `contributorCounts` | ContributorCountResponseDto[] | - | 贡献者统计 |
| `createdAt` | DateTime | ✅ | 创建日期 |
| `description` | String | ✅ | 相册描述 |
| `endDate` | DateTime | - | 结束日期（最新资源） |
| `hasSharedLink` | Boolean | ✅ | 是否有共享链接 |
| `id` | String | ✅ | 相册 ID |
| `isActivityEnabled` | Boolean | ✅ | 是否启用活动动态 |
| `lastModifiedAssetTimestamp` | DateTime | - | 最后修改资源时间戳 |
| `order` | AssetOrder | - | 资源排序顺序 |
| `owner` | UserResponseDto | ✅ | 所有者用户信息 |
| `ownerId` | String | ✅ | 所有者用户 ID |
| `shared` | Boolean | ✅ | 是否为共享相册 |
| `startDate` | DateTime | - | 开始日期（最早资源） |
| `updatedAt` | DateTime | ✅ | 最后更新日期 |

---

## 历史版本 (History)

| 版本 | 状态 |
|------|------|
| v2 | Stable |
| v1 | Beta |
| v1 | Added |

---

## 相关链接

- **上一个端点**: Albums.deleteAlbum
- **下一个端点**: Albums.addAssetsToAlbum

---

> 来源: https://api.immich.app/endpoints/albums/updateAlbumInfo
