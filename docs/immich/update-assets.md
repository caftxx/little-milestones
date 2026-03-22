# Update assets

> 批量更新资源

同时更新多个资源。

---

**状态**: Stable
**权限**: `asset.update`

---

## 端点信息

| 属性 | 值 |
|------|-----|
| 方法 | `PUT` |
| 路径 | `/assets` |
| 名称 | `updateAssets` |

---

## 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| `dateTimeOriginal` | String | ❌ | 原始日期和时间 |
| `dateTimeRelative` | Number | ❌ | 相对时间偏移（秒） |
| `description` | String | ❌ | 资源描述 |
| `duplicateId` | String \| Null | ❌ | 重复 ID |
| `ids` | UUID[] | ✅ | 要更新的资源 ID 列表 |
| `isFavorite` | Boolean | ❌ | 标记为收藏 |
| `latitude` | Number | ❌ | 纬度坐标 |
| `longitude` | Number | ❌ | 经度坐标 |
| `rating` | Number \| Null | ❌ | 评分范围 [1-5]，null 表示未评分 |
| `timeZone` | String | ❌ | 时区（IANA 时区格式） |
| `visibility` | AssetVisibility | ❌ | 资源可见性 |

---

## 响应

| 状态码 | 类型 | 描述 |
|--------|------|------|
| `204` | No content | 无内容（更新成功） |

---

## 历史版本

| 版本 | 状态 |
|------|------|
| v2 | Stable |
| v1 | Beta |
| v1 | Added |

---

## 相关链接

- **上一个端点**: [Assets.addUsersToAlbum](https://api.immich.app/endpoints/assets/addUsersToAlbum)
- **下一个端点**: [Assets.uploadAsset](https://api.immich.app/endpoints/assets/uploadAsset)

---

> 来源: https://api.immich.app/endpoints/assets/updateAssets
