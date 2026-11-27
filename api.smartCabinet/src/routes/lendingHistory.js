const sql = require("mssql");
const { getPool } = require("../db");

const Equipment_Tool_PC =
  process.env.Equipment_Tool_PC || "[MasterKey].[dbo].[Equipment_Tool_PC]";

const Equipment_Tool_PC_LendingHistory =
  process.env.Equipment_Tool_PC_LendingHistory ||
  "[MasterKey].[dbo].[Equipment_Tool_PC_LendingHistory]";

// Quy ước Using_Status của bảng thiết bị: 'In Use' = trong tủ (sẵn sàng) | 'Lending' = đang mượn
const STATUS_IN_CABINET = "In Use";
const STATUS_LENDING = "Lending";

const serverError = (res, error) => {
  console.error("LỖI SQL:", error);
  return res.status(500).json({
    code: 500,
    result: "NG",
    message: "Lỗi kết nối cơ sở dữ liệu",
  });
};

/**
 * Đọc thiết bị theo tag và KHÓA dòng đó tới hết transaction
 * (2 người quét cùng lúc không thể cùng mượn / cùng trả 1 thiết bị).
 */
const lockEquipment = async (transaction, rfid) => {
  const result = await new sql.Request(transaction)
    .input("RFID_codes", sql.VarChar, rfid)
    .query(`
      SELECT TOP 1 *
      FROM ${Equipment_Tool_PC} WITH (UPDLOCK, HOLDLOCK)
      WHERE LTRIM(RTRIM(RFID_codes)) = @RFID_codes
    `);
  return result.recordset[0];
};

/**
 * Thiết bị không tồn tại -> 404; sai trạng thái -> 409 kèm trạng thái hiện tại.
 */
const rejectEquipment = async (transaction, res, equipment, expected) => {
  await transaction.rollback();
  if (!equipment) {
    return res.status(404).json({ code: 404, result: "NG", message: "Không tìm thấy thiết bị" });
  }
  return res.status(409).json({
    code: 409,
    result: "NG",
    message: `Thiết bị ${equipment.controlNo} đang ở trạng thái '${equipment.Using_Status}' (cần '${expected}')`,
    Using_Status: equipment.Using_Status,
  });
};

/**
 * Thêm lịch sử mượn thiết bị (giữ cho tương thích; Pi bản mới dùng borrowEquipment)
 */
const insertLendingHistory = async (req, res) => {
  try {
    const pool = await getPool();

    const {
      controlNo,
      Category,
      equipmentName,
      borrower,
      Borrow_Name,
      Code_Solenoid,
      borrowDate,
      Dept,
      Location,
    } = req.body;

    if (!controlNo || !borrower || !Code_Solenoid || !borrowDate) {
      return res.status(400).json({
        code: 400,
        result: "NG",
        message: "Thiếu thông tin bắt buộc",
      });
    }

    const query = `
      INSERT INTO ${Equipment_Tool_PC_LendingHistory}
      (controlNo, Category, equipmentName, borrower, Borrow_Name, Code_Solenoid, borrowDate, Dept, Cabinet)
      VALUES
      (@controlNo, @Category, @equipmentName, @borrower, @Borrow_Name, @Code_Solenoid, @borrowDate, @Dept, @Location)
    `;

    const result = await pool
      .request()
      .input("controlNo", sql.VarChar, controlNo)
      .input("Category", sql.VarChar, Category)
      .input("equipmentName", sql.VarChar, equipmentName)
      .input("borrower", sql.VarChar, borrower)
      .input("Borrow_Name", sql.NVarChar, Borrow_Name)
      .input("Code_Solenoid", sql.VarChar, Code_Solenoid)
      .input("borrowDate", sql.DateTime, borrowDate)
      .input("Dept", sql.VarChar, Dept)
      .input("Location", sql.VarChar, Location)
      .query(query);

    if (result.rowsAffected[0] === 0) {
      return res.status(500).json({
        code: 500,
        result: "NG",
        message: "Không thể thêm lịch sử mượn thiết bị",
      });
    }

    return res.json({
      code: 200,
      result: "OK",
      message: "Thêm lịch sử mượn thiết bị thành công",
      rowsAffected: result.rowsAffected[0],
    });
  } catch (error) {
    return serverError(res, error);
  }
};

/**
 * MƯỢN thiết bị: In Use -> Lending + ghi lịch sử, trong 1 transaction.
 * Body (Pi gửi): RFID_codes, Code, Borrow_Name, Code_Solenoid, Dept, Location, time
 */
const borrowEquipment = async (req, res) => {
  const { RFID_codes, Code, Borrow_Name, Code_Solenoid, Dept, Location, time } = req.body;
  const rfid = String(RFID_codes || "").trim();

  if (!rfid || !Code || !Code_Solenoid || !time) {
    return res.status(400).json({ code: 400, result: "NG", message: "Thiếu dữ liệu đầu vào" });
  }

  let transaction;
  try {
    const pool = await getPool();
    transaction = new sql.Transaction(pool);
    await transaction.begin();

    const equipment = await lockEquipment(transaction, rfid);
    if (!equipment || equipment.Using_Status !== STATUS_IN_CABINET) {
      return rejectEquipment(transaction, res, equipment, STATUS_IN_CABINET);
    }

    // 1) Thiết bị: In Use -> Lending
    await new sql.Request(transaction)
      .input("RFID_codes", sql.VarChar, rfid)
      .input("Code", sql.VarChar, Code)
      .input("Dept", sql.VarChar, Dept)
      .input("time", sql.DateTime, time)
      .query(`
        UPDATE ${Equipment_Tool_PC}
        SET
            Using_Status = '${STATUS_LENDING}',
            LENDING_Requester = @Code,
            LENDING_requestDept = @Dept,
            LENDING_requestDate = @time,
            LENDING_verifyDate = @time,
            Status_RFID = 1
        WHERE LTRIM(RTRIM(RFID_codes)) = @RFID_codes
          AND Using_Status = '${STATUS_IN_CABINET}'
      `);

    // 2) Lịch sử mượn (lấy controlNo / tên / loại từ chính dòng thiết bị, không tin dữ liệu client)
    await new sql.Request(transaction)
      .input("controlNo", sql.VarChar, equipment.controlNo)
      .input("Category", sql.VarChar, equipment.Category ?? equipment.Category1 ?? null)
      .input("equipmentName", sql.NVarChar, equipment.NameOf_Equipment ?? equipment.controlNo)
      .input("borrower", sql.VarChar, Code)
      .input("Borrow_Name", sql.NVarChar, Borrow_Name)
      .input("Code_Solenoid", sql.VarChar, Code_Solenoid)
      .input("borrowDate", sql.DateTime, time)
      .input("Dept", sql.VarChar, Dept)
      .input("Location", sql.VarChar, Location || equipment.Cabinet)
      .query(`
        INSERT INTO ${Equipment_Tool_PC_LendingHistory}
        (controlNo, Category, equipmentName, borrower, Borrow_Name, Code_Solenoid, borrowDate, Dept, Cabinet)
        VALUES
        (@controlNo, @Category, @equipmentName, @borrower, @Borrow_Name, @Code_Solenoid, @borrowDate, @Dept, @Location)
      `);

    await transaction.commit();

    return res.json({
      code: 200,
      result: "OK",
      message: `Mượn thành công: ${equipment.controlNo}`,
      controlNo: equipment.controlNo,
    });
  } catch (error) {
    if (transaction) await transaction.rollback().catch(() => {});
    return serverError(res, error);
  }
};

/**
 * TRẢ thiết bị: Lending -> In Use + đóng lịch sử mượn đang mở, trong 1 transaction.
 * Body (Pi gửi): RFID_codes, Code, Code_Solenoid, Dept, controlNo, time
 */
const updateStatusReturned = async (req, res) => {
  const { Code, Dept, RFID_codes, time } = req.body;
  const rfid = String(RFID_codes || "").trim();

  if (!rfid || !Code || !time) {
    return res.status(400).json({ code: 400, result: "NG", message: "Thiếu dữ liệu đầu vào" });
  }

  let transaction;
  try {
    const pool = await getPool();
    transaction = new sql.Transaction(pool);
    await transaction.begin();

    const equipment = await lockEquipment(transaction, rfid);
    if (!equipment || equipment.Using_Status !== STATUS_LENDING) {
      // Pi quét lặp sau khi đã trả -> 409 "đang In Use", không ghi trùng
      return rejectEquipment(transaction, res, equipment, STATUS_LENDING);
    }

    // 1) Thiết bị: Lending -> In Use, xóa thông tin người đang mượn
    await new sql.Request(transaction)
      .input("RFID_codes", sql.VarChar, rfid)
      .input("Code", sql.VarChar, Code)
      .input("Dept", sql.VarChar, Dept)
      .input("time", sql.DateTime, time)
      .query(`
        UPDATE ${Equipment_Tool_PC}
        SET
            Using_Status = '${STATUS_IN_CABINET}',
            RETURN_Requester = @Code,
            RETURN_requestDept = @Dept,
            RETURN_requestDate = @time,
            RETURN_verifyDate = @time,
            LENDING_Requester = NULL,
            LENDING_requestDept = NULL,
            LENDING_requestDate = NULL,
            LENDING_verifyDate = NULL,
            Status_RFID = 0
        WHERE LTRIM(RTRIM(RFID_codes)) = @RFID_codes
          AND Using_Status = '${STATUS_LENDING}'
      `);

    // 2) Đóng lịch sử đang mở của thiết bị (ai trả cũng được; người trả ghi ở returnVerifier)
    const hist = await new sql.Request(transaction)
      .input("controlNo", sql.VarChar, equipment.controlNo)
      .input("Code", sql.VarChar, Code)
      .input("time", sql.DateTime, time)
      .query(`
        UPDATE ${Equipment_Tool_PC_LendingHistory}
        SET
            returnStatus = 'Returned',
            returnDate = @time,
            returnVerifier = @Code,
            verifyReturnDate = @time
        WHERE controlNo = @controlNo
          AND returnStatus IS NULL
      `);

    await transaction.commit();

    return res.json({
      code: 200,
      result: "OK",
      message: `Trả thành công: ${equipment.controlNo}`,
      controlNo: equipment.controlNo,
      historyClosed: hist.rowsAffected[0], // 0 -> Pi báo "trả được nhưng không thấy lịch sử mượn"
    });
  } catch (error) {
    if (transaction) await transaction.rollback().catch(() => {});
    return serverError(res, error);
  }
};

module.exports = {
  insertLendingHistory,
  borrowEquipment,
  updateStatusReturned,
};
