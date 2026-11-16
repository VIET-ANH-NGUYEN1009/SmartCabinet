const sql = require("mssql");
const { getPool } = require("../db");

const Equipment_Tool_PC =
  process.env.Equipment_Tool_PC || "[MasterKey].[dbo].[Equipment_Tool_PC]";

const CabinetCount = async (req, res) => {
  try {
    const pool = await getPool();
    const query = `
      SELECT
    Cabinet,
    COUNT(*) AS TotalEquipment,
    (SELECT COUNT(DISTINCT Cabinet)
     FROM Equipment_Tool_PC
     WHERE Cabinet IS NOT NULL) AS TotalCabinet
FROM Equipment_Tool_PC
WHERE Cabinet IS NOT NULL
GROUP BY Cabinet
ORDER BY Cabinet;
    `;

    const result = await pool.request().query(query);

    return res.json({
      code: 200,
      result: "OK",
      data: result.recordset,
    });
  } catch (error) {
    console.error("LỖI SQL:", error);
    return res.status(500).json({
      code: 500,
      result: "NG",
      message: "Lỗi kết nối cơ sở dữ liệu",
      detail: error.message,
    });
  }
};

const EquipmentCount = async (req, res) => {
  try {
    const pool = await getPool();

    const { Cabinet } = req.query;

    if (!Cabinet?.trim()) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "Cabinet không được để trống",
      });
    }

    const query = `
      SELECT 
    COUNT(*) AS [Total_Equipment],
    SUM(CASE WHEN [Using_Status] = 'Lending' THEN 1 ELSE 0 END) AS [Lending_Count],
    SUM(CASE WHEN [Using_Status] = 'In Use' THEN 1 ELSE 0 END) AS [In_Use_Count],
    COUNT(DISTINCT [Layer]) AS [LayerCOUNT],
    COUNT(DISTINCT CASE WHEN [Layer] = '1' THEN [Cabinet_Slot] END) AS [Layer1_SlotCount],
    COUNT(DISTINCT CASE WHEN [Layer] = '2' THEN [Cabinet_Slot] END) AS [Layer2_SlotCount],
    COUNT(DISTINCT CASE WHEN [Layer] = '3' THEN [Cabinet_Slot] END) AS [Layer3_SlotCount],
    COUNT(DISTINCT CASE WHEN [Layer] = '4' THEN [Cabinet_Slot] END) AS [Layer4_SlotCount]
FROM [MasterKey].[dbo].[Equipment_Tool_PC]
WHERE [Cabinet] = @Cabinet 
  AND [Borrow] = 'Need Borrow'

    `;

    const result = await pool
      .request()
      .input("Cabinet", sql.VarChar, Cabinet.trim())
      .query(query);

    return res.json({
      code: 200,
      result: "OK",
      data: result.recordset[0],
    });
  } catch (error) {
    console.error("LỖI SQL:", error);
    return res.status(500).json({
      code: 500,
      result: "NG",
      message: "Lỗi kết nối cơ sở dữ liệu",
      detail: error.message,
    });
  }
};

const getEquipment_table = async (req, res) => {
  try {
    const pool = await getPool();

    const { Cabinet } = req.query;

    if (!Cabinet?.trim()) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "Cabinet không được để trống",
      });
    }

    const query = `
      SELECT *
      FROM ${Equipment_Tool_PC}
      WHERE Cabinet = @Cabinet
        AND Borrow = 'Need Borrow'
    `;

    const result = await pool
      .request()
      .input("Cabinet", sql.VarChar, Cabinet.trim())
      .query(query);

    if (result.recordset.length === 0) {
      return res.status(404).json({
        code: 404,
        result: "NG",
        message: "Không tìm thấy thiết bị với Cabinet đã cho",
      });
    }

    return res.json({
      code: 200,
      result: "OK",
      data: result.recordset,
    });
  } catch (error) {
    console.error("LỖI SQL:", error);

    return res.status(500).json({
      code: 500,
      result: "NG",
      message: "Lỗi kết nối cơ sở dữ liệu",
      detail: error.message,
    });
  }
};

/**
 * Lấy thiết bị đang được mượn
 */
const getEquipment_lending = async (req, res) => {
  try {
    const pool = await getPool();

    const { Cabinet } = req.query;

    if (!Cabinet?.trim()) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "Cabinet không được để trống",
      });
    }

    const query = `
      SELECT *
      FROM ${Equipment_Tool_PC}
      WHERE Cabinet = @Cabinet
        AND Borrow = 'Need Borrow'
        AND Using_Status = 'Lending'
    `;

    const result = await pool
      .request()
      .input("Cabinet", sql.VarChar, Cabinet.trim())
      .query(query);

    if (result.recordset.length === 0) {
      return res.status(404).json({
        code: 404,
        result: "NG",
        message: "Không tìm thấy thiết bị đang cho mượn",
      });
    }

    return res.json({
      code: 200,
      result: "OK",
      data: result.recordset,
    });
  } catch (error) {
    console.error("LỖI SQL:", error);

    return res.status(500).json({
      code: 500,
      result: "NG",
      message: "Lỗi kết nối cơ sở dữ liệu",
      detail: error.message,
    });
  }
};

/**
 * Borrow thiết bị
 */
const updateEquipment_Lending_Status = async (req, res) => {
  try {
    const pool = await getPool();

    const { RFID_codes, Code, Dept, time } = req.body;

    if (!RFID_codes) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "RFID_codes không được để trống",
      });
    }

    const query = `
      UPDATE ${Equipment_Tool_PC}
      SET
          Using_Status = 'Lending',
          LENDING_Requester = @Code,
          LENDING_requestDept = @Dept,
          LENDING_requestDate = @time,
          LENDING_verifyDate = @time
      WHERE RFID_codes LIKE '%' + @RFID_codes + '%'
    `;

    const result = await pool
      .request()
      .input("Code", sql.VarChar, Code)
      .input("Dept", sql.VarChar, Dept)
      .input("time", time)
      .input("RFID_codes", sql.VarChar, RFID_codes)
      .query(query);

    if (result.rowsAffected[0] === 0) {
      return res.status(404).json({
        code: 404,
        result: "NG",
        message: "Không tìm thấy thiết bị",
      });
    }

    return res.json({
      code: 200,
      result: "OK",
      message: "Cập nhật trạng thái thành công",
      rowsAffected: result.rowsAffected[0],
    });
  } catch (error) {
    console.error("LỖI SQL:", error);

    return res.status(500).json({
      code: 500,
      result: "NG",
      message: "Lỗi kết nối cơ sở dữ liệu",
      detail: error.message,
    });
  }
};

const updateEquipment_Return_Status = async (req, res) => {
  try {
    const pool = await getPool();

    const { RFID_codes, Code, Dept, time } = req.body;

    if (!RFID_codes) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "RFID_codes không được để trống",
      });
    }

    const query = `
      UPDATE ${Equipment_Tool_PC}
      SET
          Using_Status = 'In Use',
          RETURN_Requester = @Code,
          RETURN_requestDept = @Dept,
          RETURN_requestDate = @time,
          RETURN_verifyDate = @time
          LENDING_Requester = NULL
          LENDING_requestDept = NULL
          LENDING_requestDate = NULL
          LENDING_verifyDate = NULL

      WHERE RFID_codes LIKE '%' + @RFID_codes + '%'
    `;

    const result = await pool
      .request()
      .input("Code", sql.VarChar, Code)
      .input("Dept", sql.VarChar, Dept)
      .input("time", time)
      .input("RFID_codes", sql.VarChar, RFID_codes)
      .query(query);

    if (result.rowsAffected[0] === 0) {
      return res.status(404).json({
        code: 404,
        result: "NG",
        message: "Không tìm thấy thiết bị",
      });
    }

    return res.json({
      code: 200,
      result: "OK",
      message: "Trả thiết bị thành công",
      rowsAffected: result.rowsAffected[0],
    });
  } catch (error) {
    console.error("LỖI SQL:", error);

    return res.status(500).json({
      code: 500,
      result: "NG",
      message: "Lỗi kết nối cơ sở dữ liệu",
      detail: error.message,
    });
  }
};

module.exports = {
  CabinetCount,
  getEquipment_table,
  getEquipment_lending,
  updateEquipment_Lending_Status,
  updateEquipment_Return_Status,
  EquipmentCount,
};
